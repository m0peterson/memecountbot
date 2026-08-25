# The warning asset

Drop the image the bot should reply with in this folder and point `WARNING_FILE`
at it (default: `assets/warning.jpg`).

Anything Telegram accepts works:

| File            | Sent as     |
|-----------------|-------------|
| `warning.jpg` / `.png` | photo    |
| `warning.webp` / `.tgs` | sticker |
| `warning.gif` / `.mp4` | animation |

You can also skip the file entirely and set `WARNING_FILE` to a URL or to the
`file_id` of something already uploaded to Telegram. To grab a `file_id`: send
the image to [@RawDataBot](https://t.me/RawDataBot) and copy the `file_id` field
from its reply.

If no file is found, the bot falls back to replying with `WARNING_TEXT`, so it
works out of the box.

The file is read at startup, so restart the bot after adding or replacing it.
