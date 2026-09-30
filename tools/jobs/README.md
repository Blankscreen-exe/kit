# jobs

Open the job tracker, the local app for companies you're interested in and the jobs you've applied to, or run any of its jobs commands.

The job tracker is a separate project; this tool only launches its `jobs` command. All arguments are passed through.

## Usage

```
kit jobs                        # open the tracker in your browser
kit jobs serve --port 9000      # use another port; --no-open to skip the browser
kit jobs import sheet.csv       # import a spreadsheet (add --dry-run to preview)
kit jobs export companies       # write companies as CSV (also: applications, backup)
kit jobs where                  # show where the data lives
```

kit runs the `jobs` command if it's on PATH (the tracker's `bin/` folder). Otherwise it runs `jobs.py`
from the checkout named by the `project` setting, with kit's own Python.

## Settings

| Setting | Env | Meaning |
|---|---|---|
| `jobs.project` | `JOB_TRACKER_PROJECT` | Folder of a job-application-tracker checkout, for when `jobs` is not on PATH |

The tracker's data location is its own setting: `JOBTRACKER_HOME`.

## Examples

```
kit config set jobs.project ~/CODE_LAB/job-application-tracker
kit jobs
```
