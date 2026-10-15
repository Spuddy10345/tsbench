class Fatal(Exception):
    """Unrecoverable error; the message is printed and we exit non-zero."""


# Exit codes shared by the subcommands (0 = success, 1 = error, 2 = bad usage,
# 4 = no history to act on, 130 = interrupted).
EXIT_SKIPPED = 10      # a scheduled run chose not to start (a guard said no)
EXIT_LOCKED = 11       # another tsbench run holds the lock
