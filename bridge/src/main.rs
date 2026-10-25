//! ts-bridge: a headless TeamSpeak 3 client.
//!
//! Joins a server, receives the channel's voice, decodes it per speaker (Opus → PCM) and writes
//! each utterance to stdout: one JSON line, then `samples` × 2 bytes of 16-bit PCM, 16 kHz, mono.
//! Logs go to stderr. Full protocol: docs/ARCHITECTURE.md §3.

use std::collections::HashMap;
use std::io::Write;
use std::sync::mpsc::{channel, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use futures::prelude::*;
use serde_json::{json, Value};
use tsclientlib::audio::AudioHandler;
use tsclientlib::{ClientId, Connection, DisconnectOptions, Identity, StreamItem};
use tsproto_packets::packets::AudioData;

/// 20 ms of 48 kHz interleaved stereo: the AudioHandler's output format.
const TICK_SAMPLES: usize = 48_000 / 50 * 2;
/// 3 stereo frames at 48 kHz = 1 mono sample at 16 kHz.
const DECIMATION: usize = 6;

struct Speech {
	started_ms: u128,
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
	})
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
				buf.fill(0.0);
				let ended = audio.fill_buffer_with_proc(&mut buf, |id, samples| {
					let s = talking.entry(*id).or_insert_with(|| Speech { started_ms: now_ms(), pcm: Vec::new() });
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
							"speaker": speaker(&con, *id),
							"started_ms": s.started_ms as u64,
							"samples": s.pcm.len(),
						});
						emit(&out, header, &s.pcm);
					}
					!done
				});
			}
			Some(None) => bail!("connection closed by the server"),
			Some(Some(e)) => {
				// Whispers (S2CWhisper) are private: never decoded, never archived.
				if let StreamItem::Audio(packet) = e? {
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
