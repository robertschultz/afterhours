# Native audio analysis bridge

This optional VirtualDJ plugin measures decoded audio on a paused, isolated deck. It writes numerical reports to the configured analysis directory. The plugin passes audio through unchanged and does not store PCM.

On an Apple Silicon Mac with Xcode Command Line Tools, obtain `VdjPlugin8.h` and `vdjDsp8.h` from the [official VirtualDJ SDK](https://virtualdj.com/wiki/Plugins_SDKv8.html). Keep the headers together and pass their directory explicitly:

```sh
.venv/bin/python native/analysis-bridge/build.py --sdk /path/to/sdk
```

Set `AFTERHOURS_ANALYSIS_REPORTS_DIR` before building to select the report destination. The installed bundle keeps that path. If you start Afterhours with a different data root, set the same reports directory in its environment so it can read the bridge's scans. The build writes a locally signed bundle. To install it, quit VirtualDJ and run `./install-analysis-bridge --sdk /path/to/sdk`.

The SDK headers are supplied separately under the vendor's terms. Raw audio access and four-deck isolation should be checked on the intended host.
