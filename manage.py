#!/usr/bin/env python
import os
import sys


def main():
    # Tests always run against config.settings.test; everything else defaults to dev.
    default = "config.settings.test" if "test" in sys.argv[1:2] else "config.settings.dev"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", default)
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
