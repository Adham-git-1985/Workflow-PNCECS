import os
import sys


def is_flask_db_command(arguments=None):
    selected_arguments = sys.argv[1:] if arguments is None else arguments
    return any(
        str(argument).strip().casefold() == "db"
        for argument in selected_arguments
    )


def should_run_runtime_schema_sync(arguments=None, environment=None):
    selected_environment = os.environ if environment is None else environment
    return (
        not selected_environment.get("SKIP_RUNTIME_SCHEMA")
        and not is_flask_db_command(arguments)
    )
