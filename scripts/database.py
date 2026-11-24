import argparse
import os
import subprocess
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict


def main():
    parser = argparse.ArgumentParser(description="Back up or restore the PostgreSQL store")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    environment = os.environ.copy()
    variable = "DATABASE_URL" if args.action == "backup" else "RESTORE_DATABASE_URL"
    if not environment.get(variable):
        parser.error(f"{variable} must be set")
    connection = conninfo_to_dict(environment.pop(variable))
    database = connection.pop("dbname", None)
    if not database:
        parser.error(f"{variable} must identify a database")
    for option, value in connection.items():
        environment["PG" + option.upper().replace("_", "")] = value
    environment["PGDATABASE"] = database
    if args.action == "backup":
        with args.file.open("xb") as output:
            try:
                subprocess.run(["pg_dump", "--format=custom"], env=environment, stdout=output, check=True)
            except BaseException:
                output.close()
                args.file.unlink()
                raise
    else:
        subprocess.run(["pg_restore", "--single-transaction", "--exit-on-error", "--dbname",
                        database, str(args.file)], env=environment, check=True)


if __name__ == "__main__":
    main()

