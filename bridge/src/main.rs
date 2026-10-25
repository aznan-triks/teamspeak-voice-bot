//! ts-bridge: a headless TeamSpeak 3 client.
//!
//! Joins a server, receives the channel's voice, decodes it per speaker (Opus → PCM) and writes
//! each utterance to stdout: one JSON line, then `samples` × 2 bytes of 16-bit PCM, 16 kHz, mono.
//! Reads `{"cmd":"play","samples":N}` + N samples of 16-bit PCM, 48 kHz, mono on stdin and says
//! them in the channel; `{"cmd":"message","client_id":N,"text":"...","poke":true}` sends a private text
//! message (and a poke, which pops up in the client);
//! `{"cmd":"clients"}` answers with the list of connected clients. Private messages to the bot become
//! `{"event":"text"}` lines. Logs go to stderr.
//! Full protocol: docs/ARCHITECTURE.md §3.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, Read, Write};
use std::sync::mpsc::{channel, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use audiopus::coder::Encoder;
use audiopus::{Application, Channels, SampleRate};
use futures::prelude::*;
use serde_json::{json, Value};
use tsclientlib::audio::AudioHandler;
use tsclientlib::{ClientId, Connection, DisconnectOptions, Identity, OutCommandExt, StreamItem};
use tsproto_packets::packets::{AudioData, CodecType, OutAudio};

/// 20 ms of 48 kHz interleaved stereo: the AudioHandler's output format.
const TICK_SAMPLES: usize = 48_000 / 50 * 2;
/// 3 stereo frames at 48 kHz = 1 mono sample at 16 kHz.
const DECIMATION: usize = 6;
/// 20 ms of 48 kHz mono: one Opus frame of the bot's voice.
const SEND_FRAME: usize = 48_000 / 50;

enum Command {
	Play(Vec<i16>),
	Message(u16, String, bool),
	Clients,
}

/// Reads commands from stdin until it closes.
fn read_commands(commands: Sender<Command>) -> Result<()> {
	let mut stdin = std::io::stdin().lock();
	let mut line = String::new();
	while stdin.read_line(&mut line)? > 0 {
		let cmd: Value = serde_json::from_str(&line).context("invalid command line on stdin")?;
		let samples = cmd["samples"].as_u64().unwrap_or(0) as usize;
		let mut bytes = vec![0u8; samples * 2];
		stdin.read_exact(&mut bytes)?;
		let command = match cmd["cmd"].as_str() {
			Some("play") => Command::Play(bytes.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]])).collect()),
			Some("message") => Command::Message(
				cmd["client_id"].as_u64().unwrap_or(0) as u16,
				cmd["text"].as_str().unwrap_or("").to_string(),
				cmd["poke"].as_bool().unwrap_or(false),
			),
			Some("clients") => Command::Clients,
			other => {
				tracing::warn!(?other, "unknown command ignored");
				line.clear();
				continue;
			}
		};
		let _ = commands.send(command);
		line.clear();
	}
	Ok(())
}

struct Speech {
	started_ms: u128,
	who: Value, // read when the person starts talking: they may have left by the time they stop
	pcm: Vec<i16>,
}

fn env(key: &str, default: &str) -> String { std::env::var(key).unwrap_or_else(|_| default.into()) }

fn now_ms() -> u128 { SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis()) }

/// Same file = same TeamSpeak UID: the bot keeps its profile and permissions across restarts.
fn load_identity(path: &str) -> Result<Identity> {
	if let Ok(s) = std::fs::read_to_string(path) {
		return serde_json::from_str(&s).with_context(|| format!("unreadable identity file: {path}"));
	}
	let id = Identity::create();
	std::fs::write(path, serde_json::to_string(&id)?).with_context(|| format!("cannot write {path}"))?;
	Ok(id)
}

fn emit(out: &Sender<Vec<u8>>, header: Value, pcm: &[i16]) {
	let mut frame = header.to_string().into_bytes();
	frame.push(b'\n');
	for s in pcm {
		frame.extend_from_slice(&s.to_le_bytes());
	}
	let _ = out.send(frame);
}

fn speaker(con: &Connection, id: u16) -> Value {
	let Ok(state) = con.get_state() else { return json!({ "client_id": id }) };
	let Some(c) = state.clients.get(&ClientId(id)) else { return json!({ "client_id": id }) };
	let groups: Vec<String> = c
		.server_groups
		.iter()
		.map(|g| state.server_groups.get(g).map_or_else(|| g.0.to_string(), |sg| sg.name.clone()))
		.collect();
	json!({
		"client_id": id,
		"uid": c.uid.as_ref().map(|u| u.to_string()),
		"nickname": c.name,
		"server_groups": groups,
		"channel_id": c.channel.0,
		"hearing": !c.output_muted && c.output_hardware_enabled, // headset on, sound not muted
	})
}

/// UIDs of the people who could hear the channel right now: in the bot's channel, sound on.
fn listeners(con: &Connection) -> Vec<String> {
	let Ok(state) = con.get_state() else { return Vec::new() };
	let Some(channel) = state.clients.get(&state.own_client).map(|me| me.channel) else { return Vec::new() };
	state
		.clients
		.values()
		.filter(|c| c.id != state.own_client && c.channel == channel && !c.output_muted && c.output_hardware_enabled)
		.filter_map(|c| c.uid.as_ref().map(|u| u.to_string()))
		.collect()
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
	tracing_subscriber::fmt().with_writer(std::io::stderr).init();
	let max_samples = env("TS_MAX_UTTERANCE_S", "30").parse::<usize>()? * 16_000;

	let mut opts = Connection::build(env("TS_ADDRESS", "localhost"))
		.name(env("TS_NICKNAME", "VoiceBot"))
		.identity(load_identity(&env("TS_IDENTITY_FILE", "identity.json"))?);
	if let Ok(c) = std::env::var("TS_CHANNEL") {
		opts = opts.channel(c);
	}
	if let Ok(p) = std::env::var("TS_CHANNEL_PASSWORD") {
		opts = opts.channel_password(p);
	}
	if let Ok(p) = std::env::var("TS_SERVER_PASSWORD") {
		opts = opts.password(p);
	}
	let mut con = opts.connect()?;

	// Wait for the server's initial state (clients, groups) before announcing "ready".
	if let Some(r) = con.events().try_filter(|e| future::ready(matches!(e, StreamItem::BookEvents(_)))).next().await {
		r?;
	}
	// The bot itself, then the server group names, only arrive after the initial state.
	let full_state = async {
		loop {
			let state = con.get_state()?;
			if state.clients.contains_key(&state.own_client) && !state.server_groups.is_empty() {
				return anyhow::Ok(());
			}
			match con.events().next().await {
				Some(r) => drop(r?),
				None => bail!("connection closed before the client list arrived"),
			}
		}
	};
	// Without permission to list server groups, carry on with their numeric ids.
	match tokio::time::timeout(Duration::from_secs(5), full_state).await {
		Ok(r) => r?,
		Err(_) => tracing::warn!("server group list not received: numeric ids are used instead of names"),
	}

	// Write from a dedicated thread: a slow orchestrator must never block the network loop.
	// shortcut: unbounded queue, RAM grows if the orchestrator falls behind; bound it if that happens.
	let (out, frames) = channel::<Vec<u8>>();
	std::thread::spawn(move || {
		let mut stdout = std::io::stdout().lock();
		for frame in frames {
			if stdout.write_all(&frame).and_then(|_| stdout.flush()).is_err() {
				std::process::exit(0); // the orchestrator is gone: nothing left to do
			}
		}
	});
	let own = con.get_state()?.own_client.0;
	emit(&out, json!({ "event": "ready", "speaker": speaker(&con, own) }), &[]);

	let (commands_in, commands) = channel::<Command>();
	std::thread::spawn(move || {
		if let Err(error) = read_commands(commands_in) {
			tracing::error!(%error, "stdin closed or unreadable: the bot can no longer speak");
		}
	});
	let encoder = Encoder::new(SampleRate::Hz48000, Channels::Mono, Application::Voip)?;
	let mut to_say: VecDeque<i16> = VecDeque::new();
	let mut saying = false;
	let mut opus = [0u8; 1275]; // largest possible Opus packet

	let mut audio = AudioHandler::<u16>::new();
	let mut talking: HashMap<u16, Speech> = HashMap::new();
	let mut buf = vec![0f32; TICK_SAMPLES];
	let mut tick = tokio::time::interval(Duration::from_millis(20));

	loop {
		let item = {
			let mut events = con.events();
			tokio::select! {
				_ = tick.tick() => None,
				_ = tokio::signal::ctrl_c() => break,
				e = events.next() => Some(e),
			}
		};
		match item {
			None => {
				// The bot's voice: one 20 ms Opus frame per tick, so it plays at normal speed.
				for command in commands.try_iter() {
					match command {
						Command::Play(samples) => to_say.extend(samples),
						Command::Message(id, text, poke) => {
							// A poke pops up in the middle of the client: hard to miss, unlike a private chat.
							let msgs = con.get_state()?.clients.get(&ClientId(id)).map(|c| {
								let short: String = text.chars().take(100).collect(); // pokes are limited to 100 characters
								(c.send_textmessage(&text), poke.then(|| c.poke(&short)))
							});
							match msgs {
								Some((msg, poke)) => {
									msg.send(&mut con)?;
									if let Some(poke) = poke {
										poke.send(&mut con)?;
									}
								}
								None => tracing::warn!(id, "message not sent: client not found"),
							}
						}
						Command::Clients => {
							let ids: Vec<u16> = con.get_state()?.clients.keys().map(|c| c.0).collect();
							let clients: Vec<Value> = ids.into_iter().map(|id| speaker(&con, id)).collect();
							emit(&out, json!({ "event": "clients", "clients": clients }), &[]);
						}
					}
				}
				if !to_say.is_empty() {
					let mut frame: Vec<i16> = to_say.drain(..to_say.len().min(SEND_FRAME)).collect();
					frame.resize(SEND_FRAME, 0); // pad the last frame with silence
					let len = encoder.encode(&frame, &mut opus)?;
					let data = AudioData::C2S { id: 0, codec: CodecType::OpusVoice, data: &opus[..len] };
					con.send_audio(OutAudio::new(&data))?;
					saying = true;
				} else if saying {
					// An empty packet tells listeners the bot stopped talking.
					con.send_audio(OutAudio::new(&AudioData::C2S { id: 0, codec: CodecType::OpusVoice, data: &[] }))?;
					saying = false;
					emit(&out, json!({ "event": "played" }), &[]);
				}

				buf.fill(0.0);
				let ended = audio.fill_buffer_with_proc(&mut buf, |id, samples| {
					let s = talking.entry(*id).or_insert_with(|| {
						let who = speaker(&con, *id);
						emit(&out, json!({ "event": "speaking", "speaker": who }), &[]); // live view: someone starts talking
						Speech { started_ms: now_ms(), who, pcm: Vec::new() }
					});
					// shortcut: box-filter decimation (averaging), good enough for speech recognition;
					// switch to a real resampler if archived audio quality ever matters.
					s.pcm.extend(samples.chunks(DECIMATION).map(|c| {
						(c.iter().sum::<f32>() / c.len() as f32 * 32767.0).clamp(-32768.0, 32767.0) as i16
					}));
				});
				// End of speech, or a monologue cut into pieces: Whisper works on 30 s windows.
				talking.retain(|id, s| {
					let done = ended.contains(id) || s.pcm.len() >= max_samples;
					if done && !s.pcm.is_empty() {
						let header = json!({
							"event": "utterance",
							"speaker": s.who,
							"started_ms": s.started_ms as u64,
							"samples": s.pcm.len(),
							"listeners": listeners(&con), // who may read it later: those who could hear it
						});
						emit(&out, header, &s.pcm);
					}
					!done
				});
			}
			Some(None) => bail!("connection closed by the server"),
			Some(Some(e)) => {
				let e = e?;
				// Private text messages to the bot (login codes, commands typed in the chat).
				if let StreamItem::BookEvents(events) = &e {
					for event in events {
						if let tsclientlib::events::Event::Message { target: tsclientlib::MessageTarget::Client(_), invoker, message } = event {
							emit(&out, json!({ "event": "text", "speaker": speaker(&con, invoker.id.0), "text": message }), &[]);
						}
					}
				}
				// Whispers (S2CWhisper) are private: never decoded, never archived.
				if let StreamItem::Audio(packet) = e {
					let from = match packet.data().data() {
						AudioData::S2C { from, .. } => Some(*from),
						_ => None,
					};
					if let Some(from) = from {
						if let Err(error) = audio.handle_packet(from, packet) {
							tracing::debug!(%error, "audio packet dropped");
						}
					}
				}
			}
		}
	}

	con.disconnect(DisconnectOptions::new())?;
	con.events().for_each(|_| future::ready(())).await;
	Ok(())
}
