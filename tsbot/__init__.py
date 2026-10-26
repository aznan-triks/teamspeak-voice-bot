"""TeamSpeak voice bot orchestrator: Rust bridge → speech-to-text → archive → plugins."""

# Starts every log line that carries what someone said: the site's log pages leave them out, since only people
# who were in the channel may read it (journalctl on the machine still has them).
SPOKEN = "[spoken] "
