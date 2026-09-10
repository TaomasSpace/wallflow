# Addons

`wallflow addons list` · `wallflow addons install <name>` · `wallflow addons remove <name>` · `wallflow addons info <name>`

An addon is a folder with an `addon.toml` + a wallust template and/or a helper
script (`bin = …`, installed to `~/.local/bin`). Copy one to
make your own; the fields are documented at the top of `wallflow/addons.py`.
Template syntax is wallust's: `{{color0}}`…`{{color15}}`, `{{background}}`,
`{{foreground}}`, `{{cursor}}`; append `| strip` to drop the `#`.

cmatrix, tty-clock & co. take their colours from the terminal palette, which
wallust already recolours live — no addon needed for those. pipes.sh is the
exception: its screen reset drops the palette, hence the `pipes` addon.
