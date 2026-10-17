# content-machine

Start Content Machine, the local content pipeline, or run any of its `cm` commands.

## Usage

```
kit content-machine [--port N] [--lan] [--no-open]
kit content-machine <cm command> [args]
```

- With no command it runs `cm serve`: the app starts and opens in your browser. The printed address carries the
  access token. Ctrl+C stops it.
- Anything else is passed to `cm` as it is: `kit cm pieces`, `kit cm stage 12 ready`, `kit cm where`.
- It runs the `cm` in the project's own `.venv`, from the project folder, so it behaves exactly as it does there.
- In `kit hub` it has a Launch button and an Open link.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `content-machine.home` | *(none)* | The Content Machine project folder, the one with `.venv` in it. `$KIT_CONTENT_MACHINE_HOME` overrides it. |

## Examples

```
kit config set content-machine.home F:\path\to\content_machine
kit cm
kit cm --lan
kit cm pieces --brand example
```
