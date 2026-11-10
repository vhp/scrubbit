"""scrubbit: overwrite and delete your Reddit history using your Reddit data export.

The export is the only complete list of what you've posted, because Reddit's
listing endpoints stop after about 1000 items. scrubbit reads that list, overwrites
every comment and self post with filler, confirms the filler saved, then deletes
everything and confirms that too.

Written with Claude Code.

Modules:
    export: reads the export without trusting its layout.
    reddit: a small OAuth client that paces, retries and explains failures.
    shredder: the overwrite-then-delete run.
    report: the tally of outcomes and the failures file.
    cli: flags, environment variables and the entry point.
"""

__version__ = "1.0.0"
