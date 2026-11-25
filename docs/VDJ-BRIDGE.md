# VirtualDJ OSC bridge

Afterhours uses VirtualDJ's OSC interface on loopback. Install VirtualDJ and configure OSC in its settings. Check the application's licensing requirements for your setup.

Set the VirtualDJ OSC command port to `7474`. Configure replies on `7475` or return-to-sender if supported by your VirtualDJ configuration. Afterhours uses these ports by default. To select different ports, run `./start-dj --vdj-port PORT --vdj-reply-port PORT` from the repository root.

```sh
./dj doctor
./dj status
./dj play --deck 1
./dj pause --deck 1 --execute
```

Transport commands preview by default. Use `--execute` to dispatch a live change. Start with read-only checks. A successful UDP send alone does not verify a change. Keep physical audio and headphone checks separate from OSC telemetry.

For streaming catalogs, sign in and select the catalog inside VirtualDJ. Afterhours reads the supported catalog and deck APIs. The bridge does not grant access to a provider catalog or bypass track access restrictions. For feature measurements, see the [optional native bridge](../native/analysis-bridge/README.md).
