# ripdvd

Automated DVD ripper using MakeMKV. Monitors for DVDs, rips titles meeting a minimum duration threshold, ejects the disc, and waits for the next one.

## Requirements

- Python 3.11+ (or 3.7+ with `pip install tomli`)
- [MakeMKV](https://www.makemkv.com/) installed and licensed
- `makemkvcon` command-line tool (bundled with MakeMKV)

## Setup

```
pip install -r requirements.txt
```

## Usage

```
rip.py [options]
```

## Options

| Option | Description |
|--------|-------------|
| `--config FILE` | Path to config file (default: `config.toml`) |
| `--output DIR` | Output directory (overrides config) |
| `--min-duration SECONDS` | Minimum title duration in seconds (overrides config) |
| `--drive INDEX` | Drive index 0, 1, 2, etc. (overrides config) |
| `--makemkvcon-path PATH` | Path to makemkvcon executable (overrides config) |
| `--debug` | Enable debug output |

## Examples

```sh
# Use defaults from config.toml
rip.py

# Use a custom config file
rip.py --config custom.toml

# Override output directory
rip.py --output ~/MyRips

# Only rip titles 15 minutes or longer
rip.py --min-duration 900

# Rip from second drive with verbose output
rip.py --drive 1 --debug

# Specify a non-default makemkvcon path
rip.py --makemkvcon-path "/opt/makemkv/bin/makemkvcon"
```

## Configuration

See `config.toml` for all settings. CLI flags override config file values.

```toml
[makemkv]
path = "C:\\Program Files (x86)\\MakeMKV\\makemkvcon64.exe"
drive = 0

[output]
directory = "n:/Videos"

[rip]
min_duration_seconds = 1800
poll_interval_seconds = 10
query_timeout_seconds = 300

[logging]
log_file = "rip.log"
log_level = "INFO"
```

## How it works

1. Acquires a lock file to prevent duplicate instances per drive
2. Polls the disc drive until a DVD is detected
3. Queries all titles and filters by minimum duration
4. Rips qualifying titles to `{output}/{disc_label}_{timestamp}/`
5. Ejects the disc and waits for the next one
6. Loops indefinitely until interrupted with Ctrl+C