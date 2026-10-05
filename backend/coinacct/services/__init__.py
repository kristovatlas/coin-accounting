"""Orchestration (architecture §2, §3): start-up checks, the job worker, the tip poller and shutdown.
The API calls into this layer; it calls the pure engines, `chain/`, `prices/` and `storage/`."""
