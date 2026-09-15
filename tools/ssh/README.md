# ssh

List, add, remove and connect to the SSH hosts saved in your ~/.ssh/config.

## Usage

```
kit ssh [list]
kit ssh <name> [ssh args...]
kit ssh add <name> <user@host[:port]> [-p PORT] [-i KEY]
kit ssh remove <name>
kit ssh show <name> [--all]
kit ssh keys
```

- Hosts live in the standard SSH config file, so every name also works with plain `ssh`, `scp`,
  `rsync` and VS Code Remote-SSH. `Include` files are followed; wildcard entries like `Host *` aren't listed.
- `add` appends a `Host` block marked `# added by kit ssh`. `remove` deletes one host's block.
  Both back up the file first (Windows: `%LOCALAPPDATA%\kit\backups`, Linux/macOS: `~/.local/state/kit/backups`).
- `-i` accepts a key name from `~/.ssh` (e.g. `id_ed25519`) or a full path.
- `show` runs `ssh -G`, which prints the resolved settings without connecting.
- `keys` lists the public keys in `~/.ssh`, their fingerprints and which hosts use them.
- `--config PATH` (or `KIT_SSH_CONFIG`) works on a different config file.

## Examples

```
kit ssh                                          # list hosts
kit ssh add web deploy@203.0.113.10 -i id_ed25519
kit ssh add pi pi@192.168.1.50:2222
kit ssh web                                      # connect
kit ssh web -L 5432:localhost:5432               # connect with a port forward
kit ssh show web                                 # what ssh will use, without connecting
kit ssh remove pi
kit ssh keys
```
