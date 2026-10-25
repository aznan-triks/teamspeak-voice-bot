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
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
/// 48 kHz → 16 kHz: one sample in three, after the low-pass filter.
const DECIMATION: usize = 3;
/// Low-pass filter length: 127 taps put the cut between 7 and 8 kHz, so nothing above 8 kHz folds back as hiss.
const TAPS: usize = 127;
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
	pcm: Vec<f32>, // 48 kHz mono, converted to 16 kHz once the person stops
	quiet_since: Option<Instant>, // TeamSpeak stopped their voice: kept a moment in case they go on
}

/// Windowed-sinc (Blackman) low-pass, then decimation: the old 3-sample average let 8-16 kHz fold onto
/// "s", "ch" and "f" as noise, which speech-to-text heard as other words.
fn to_16k(x: &[f32]) -> Vec<i16> {
	use std::f32::consts::PI;
	let fc = 7_000.0 / 48_000.0;
	let mid = (TAPS / 2) as f32;
	let mut h: Vec<f32> = (0..TAPS)
		.map(|i| {
			let n = i as f32 - mid;
			let sinc = if n == 0.0 { 2.0 * fc } else { (2.0 * PI * fc * n).sin() / (PI * n) };
			let t = 2.0 * PI * i as f32 / (TAPS - 1) as f32;
			sinc * (0.42 - 0.5 * t.cos() + 0.08 * (2.0 * t).cos())
		})
		.collect();
	let sum: f32 = h.iter().sum();
	h.iter_mut().for_each(|v| *v /= sum); // unit gain: the volume does not change
	(0..x.len())
		.step_by(DECIMATION)
		.map(|c| {
			let start = c as isize - mid as isize;
			let acc: f32 = h
				.iter()
				.enumerate()
				.filter_map(|(k, hk)| x.get(usize::try_from(start + k as isize).ok()?).map(|v| hk * v))
				.sum();
			(acc * 32767.0).clamp(-32768.0, 32767.0) as i16
		})
		.collect()
}

#[cfg(test)]
mod tests {
	use super::*;

	fn tone(hz: f32) -> Vec<f32> {
		(0..48_000).map(|i| 0.5 * (2.0 * std::f32::consts::PI * hz * i as f32 / 48_000.0).sin()).collect()
	}

	fn level(x: &[i16]) -> f32 { x[1000..x.len() - 1000].iter().map(|&v| (v as f32).abs()).fold(0.0, f32::max) }

	#[test]
	fn speech_passes_and_high_hiss_is_removed() {
		let speech = level(&to_16k(&tone(1_000.0)));
		assert!((speech - 0.5 * 32767.0).abs() < 0.02 * 32767.0, "1 kHz kept at the same volume: {speech}");
		assert!(level(&to_16k(&tone(12_000.0))) < 0.005 * 32767.0, "12 kHz would fold onto 4 kHz");
		assert_eq!(to_16k(&tone(1_000.0)).len(), 16_000);
	}
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
	// TeamSpeak cuts the voice at every short pause: pieces closer than this are one sentence for speech-to-text.
	let join_gap = Duration::from_millis(env("TS_JOIN_GAP_MS", "1000").parse()?);

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
						Speech { started_ms: now_ms(), who, pcm: Vec::new(), quiet_since: None }
					});
					if s.quiet_since.take().is_some() {
						s.pcm.extend(std::iter::repeat(0.0).take(48_000 / 5)); // 0.2 s of silence between the pieces
					}
					s.pcm.extend(samples.chunks(2).map(|lr| lr.iter().sum::<f32>() / lr.len() as f32)); // stereo → mono
				});
				// End of speech, or a monologue cut into pieces: the speech model works on 30 s windows.
				talking.retain(|id, s| {
					if ended.contains(id) && s.quiet_since.is_none() {
						s.quiet_since = Some(Instant::now());
					}
					let done = s.quiet_since.is_some_and(|t| t.elapsed() >= join_gap) || s.pcm.len() >= max_samples * DECIMATION;
					if done && !s.pcm.is_empty() {
						let pcm = to_16k(&s.pcm);
						let header = json!({
							"event": "utterance",
							"speaker": s.who,
							"started_ms": s.started_ms as u64,
							"samples": pcm.len(),
							"listeners": listeners(&con), // who may read it later: those who could hear it
						});
						emit(&out, header, &pcm);
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
