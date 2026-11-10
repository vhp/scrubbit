# scrubbit

scrubbit overwrites and deletes your Reddit comments and posts. It works from your
Reddit data export, which lists everything you've ever posted, rather than from
Reddit's listings, which stop after about 1000 items.

Comments and self posts are overwritten with random filler first, and scrubbit
reads them back to confirm the filler saved. Only then does it delete them, and
it checks those deletions too. A deleted item can't be edited again, so if an
overwrite fails or can't be confirmed, scrubbit doesn't delete that item. It
tells you, and the next run tries the overwrite again. If an item will never
take an edit and you want it gone anyway, `--delete-if-overwrite-fails` deletes
it with its original text. Anything that failed is listed at the end and
written to `scrubbit-failures.csv`.

Written with [Claude Code](https://claude.com/claude-code).

## Before you start

1. Request your data at https://www.reddit.com/settings/data-request. Reddit
   emails you when it's ready. Download the zip and unzip it.
2. Create API credentials at https://www.reddit.com/prefs/apps: click "create
   another app", choose "script", and set the redirect URI to
   `http://localhost:8080`. The client id is the string under the app's name;
   the secret is labeled "secret".

## Install

scrubbit needs Python 3.10 or newer and has no other dependencies. From this
folder, `make install` installs a `scrubbit` command with uv, and
`make uninstall` removes it. Without uv, `pipx install .` does the same.

To run it without installing, use `uv run scrubbit ...` from this folder.

## Quick start

Do a dry run first. It logs in, reads the export and looks everything up, then
says what it would do without changing anything:

```sh
scrubbit -u YOUR_NAME -p YOUR_PASSWORD --client-id ID --client-secret SECRET \
      --export-dir ~/Downloads/reddit-export --dry-run
```

If the plan looks right, run the same command without `--dry-run`.

To avoid typing credentials, put them in a `scrubbit.env` file in the directory
you run scrubbit from. Keep this file private: it holds your password.

```sh
SCRUBBIT_USERNAME=your_name
SCRUBBIT_PASSWORD=your_password
SCRUBBIT_CLIENT_ID=abc123
SCRUBBIT_CLIENT_SECRET=def456
SCRUBBIT_EXPORT_DIR=/Users/you/Downloads/reddit-export
```

Every option has a matching `SCRUBBIT_` variable. Real environment variables
override the file, and command-line flags override both. For on/off options
like `SCRUBBIT_DRY_RUN`, use `true` or `false` (`yes`/`no`, `on`/`off` and
`1`/`0` work too). Anything else stops scrubbit with an error, so a typo can't
quietly turn off a dry run. Comments must go on their own line: text after a
value, such as `SCRUBBIT_DRY_RUN=true # test`, becomes part of the value.

If your account uses two-factor authentication, pass the password as
`password:123456` with a current code. Reddit's tokens last an hour and scrubbit
logs in again when one expires, so on a run longer than that the code will have
changed and the login fails. Split long runs with `--before` and `--after`, or
turn off 2FA while it runs.

## What a run does

For each thing type, in the order given to `--thing-types`:

1. scrubbit finds the export file and reads it, warning about anything unexpected
   in its layout.
2. It looks every item up on Reddit, 100 per request, to get the author, score
   and current text.
3. It skips items that are already deleted, that you didn't write, or that a
   filter excludes.
4. It overwrites every comment and self post, then reads them back to confirm
   the new text saved. Link posts can't be edited, so they skip this step.
5. It deletes everything whose overwrite was confirmed, then reads it all back
   to confirm it's gone. Items whose overwrite failed or couldn't be confirmed
   are not deleted. They are listed as failures, and the next run tries the
   overwrite again. Pass `--delete-if-overwrite-fails` to delete them anyway.

Saved posts and comments are looked up so the filters can apply, then unsaved.
Friends are unfriended, and no filter applies to them. Neither is overwritten
or verified.

scrubbit waits about a second between requests to stay under Reddit's rate limit,
so a comment takes roughly two seconds. An export of 10,000 comments takes
around six hours. Stopping it with Ctrl-C prints a summary of what finished, and
running it again picks up where it stopped, because finished items are
detected and skipped.

## Options

| Option | What it does |
| --- | --- |
| `-u`, `--username` | Your Reddit username. |
| `-p`, `--password` | Your Reddit password, or `password:123456` with 2FA. |
| `--client-id`, `--client-secret` | The script app's credentials. |
| `--export-dir` | The unzipped data export folder. |
| `--thing-types` | Comma-separated list from `posts`, `comments`, `saved-posts`, `saved-comments` and `friends`. The default is `posts,comments`. |
| `--before` | Only touch items created before this time. Takes a date such as `2024-01-01` or a negative duration such as `-30 days`, so `--before "-30 days"` keeps the last month. For saved items this is when the post or comment was created, not when you saved it. |
| `--after` | Only touch items created after this time. Same formats. |
| `--max-score` | Keep items with a score higher than this. Items whose score can't be read, such as those in private subreddits, are kept too. |
| `-r`, `--replacement-comment` | Overwrite everything with this fixed text instead of random filler. |
| `--skip-subreddits` | Comma-separated subreddits to leave alone. Case doesn't matter, and an `r/` prefix is fine. Use `u/yourname` for posts on your profile. Items whose subreddit can't be determined are left alone too. |
| `--only-subreddits` | Comma-separated subreddits to limit the run to. Can't be combined with `--skip-subreddits`. |
| `--skip-ids` | Comma-separated comment or post ids to leave alone, with or without the `t1_`/`t3_` prefix. |
| `--edit-only` | Overwrite comments and self posts, but delete and unsave nothing. |
| `--delete-if-overwrite-fails` | Delete items even when they couldn't be overwritten, such as those in locked or archived threads. Their original text stays as the last version Reddit has. Without this, those items are left up and reported. |
| `--dry-run` | Log what would happen and change nothing. |
| `--user-agent` | The User-Agent sent to Reddit. The default is `scrubbit/<version>`. |
| `-v`, `--verbose` | Also log why each skipped item was skipped. |

## Examples

Delete everything except the last 30 days:

```sh
scrubbit --before "-30 days"
```

Keep anything with more than 100 points:

```sh
scrubbit --max-score 100
```

Clear one subreddit only:

```sh
scrubbit --only-subreddits AskReddit
```

Clear saved items and friends as well:

```sh
scrubbit --thing-types posts,comments,saved-posts,saved-comments,friends
```

Overwrite everything with a fixed message and keep the items:

```sh
scrubbit --edit-only -r "This content was removed by its author."
```

## Reading the output

The run ends with one line per thing type and outcome, with a count:

| Outcome | Meaning |
| --- | --- |
| `overwritten, verified` | The edit saved and was read back. |
| `deleted, verified` | The item reads back as deleted. |
| `overwritten (not visible to the API)`, `deleted (not visible to the API)` | Reddit accepted the change but can't show the item, which is usual for private or banned subreddits, so it couldn't be checked. |
| `already overwritten` | The item already shows filler or your fixed text, so it wasn't edited again. |
| `skipped: already deleted` | Deleted on an earlier run. |
| `skipped: not yours` | The API says someone else wrote it. This happens when the export links to another person's post, such as a crosspost. |
| `skipped: score unknown`, `skipped: date unknown`, `skipped: subreddit unknown` | A score, date or `--skip-subreddits` filter is set and neither the export nor the API had that value. |
| `FAILED overwrite` | The edit failed, or Reddit claimed success but the old text is still there. The item isn't deleted unless `--delete-if-overwrite-fails` is set. The detail says when Reddit won't accept an edit at all, so a rerun won't help. |
| `FAILED delete`, `FAILED unsave`, `FAILED unfriend` | The action failed, or Reddit claimed success but nothing changed. |
| `FAILED look up` | Reddit couldn't be asked about the item before the run, so the filters and checks couldn't be applied. It's left unchanged, and a rerun tries again. |
| `FAILED verify` | The change was sent, but reading it back failed, so it couldn't be confirmed. Unconfirmed overwrites aren't deleted. A rerun checks again. |
| `FAILED read export` | A file couldn't be read, or no ids could be found anywhere in it. |

Each failure is also logged as it happens, with the reason, and written to
`scrubbit-failures.csv` in the current directory. Fix the cause and run scrubbit
again to retry them.

The exit code is 0 when everything worked, 1 when some items failed or an
unexpected error stopped the run, 2 for bad arguments or a refused login, and
130 when interrupted. A run that stops partway, for any of these reasons, still
prints its summary and writes the failures file.

## When Reddit changes the export format

Reddit doesn't publish a schema or version number for the export, and the
layout has changed before. scrubbit is built to keep going anyway:

- If a file isn't at its usual name, scrubbit looks for a similarly named CSV
  anywhere in the export and logs which one it used.
- Columns are matched by name, with a few alternative names each, so their
  order doesn't matter.
- If the id column is missing or holds something that isn't an id, the id is
  taken from any Reddit link in the row: a permalink, a `redd.it` short link, or
  a `t1_`/`t3_` name. The export only has to contain your links somewhere.
- Dates, subreddits and scores the export doesn't have come from the API.

Each of these is logged as a warning naming the file and column. If a run
reports `FAILED read export`, or warns that many rows have no recognizable id,
open the CSV, find the columns that hold the id and the link, and add their
names to `FIELDS` in `src/scrubbit/export.py`. File names and the words used to
find substitutes are in `KINDS` in the same file.

## Troubleshooting

`login failed` is followed by the most likely cause:

- "client id or secret is wrong": copy them again from
  https://www.reddit.com/prefs/apps.
- "username or password is wrong, or 2FA is on": check the password, and add
  `:123456` with a current code if 2FA is on.
- "Reddit refused the login": Reddit may be blocking the app or the User-Agent.
  Try another `--user-agent`, or check whether Reddit now requires approval for
  API apps.

Other failures:

- A 403 "forbidden" on an edit usually means a private or banned subreddit, or
  a locked or archived thread. scrubbit won't delete an item it couldn't
  overwrite, so these stay up and are retried on every run. To delete them
  anyway, rerun with `--delete-if-overwrite-fails`. If every edit fails with
  403, check the app and the User-Agent before doing that.
- Rate limits are handled automatically. A warning about retrying is normal;
  a failure that says "still rate limited after retries" means Reddit kept
  refusing for several minutes, and a rerun later usually works.

## Development

`make dev` creates `.venv` with pytest, ruff and mypy. `make check` formats the
code, then lints, type-checks and tests it; run it before committing.
`make test`, `make lint` and `make fmt` run those steps on their own,
`make package` builds a wheel into `dist/`, and `make clean` removes `.venv`,
`dist/` and the caches. `make help` lists everything.

The tests use an in-memory fake of Reddit's API (`tests/fake_reddit.py`) and
never touch the network.
