# Playback clock

Clients obtain the current music clock through `/api/program`. The payload reports the selected recording, current position, observed tempo, playing state, and time of observation when available.

Use fresh native readings for synchronization. When readings expire, stop extrapolating and show an unavailable state. The music engine owns the transport and transition timing. Display, guest, and performance clients follow that state without changing playback on their own.
