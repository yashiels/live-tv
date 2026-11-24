# live-tv

Free live TV playlist and XMLTV guide, rebuilt daily and published to GitHub Pages.

- Playlist: https://yashiels.github.io/live-tv/playlist.m3u
- Guide: https://yashiels.github.io/live-tv/guide.xml.gz

The playlist header points at the guide, so players that read `x-tvg-url` (OpenTV, TiviMate) pick up the guide automatically.

## Channels

Edit `channels.json`. Each entry is an [iptv-org](https://github.com/iptv-org/iptv) channel id, grouped by category. Optional fields:

- `feed`: pin a specific iptv-org feed
- `dstv`: DStv channel number, used for the guide
- `epg`: `[source, id]` guide channel from `stvp` (Samsung TV Plus), `plex` or `roku` on i.mjh.nz
- `geo`: SA-only stream; kept when the build runner is blocked outside SA
- `open`: accept any iptv-org host for this channel and skip it in the publish floor

## Build

```sh
python3 build.py
```

Only streams on the trusted host list that return a valid manifest are kept. The build refuses to publish when fewer than 85% of channels resolve or the guide has fewer than 150 programmes, so a bad upstream day leaves the previous Pages deploy in place.

Streams play directly on the device; SABC channels only play from a South African IP.
