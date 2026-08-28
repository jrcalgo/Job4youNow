"""Aurora persistence layer. `aurora_client.py` is the only module that talks
to boto3's `rds-data` client; every repository in this package calls it
through `AuroraClient.exec` / `exec_batch` / `transaction`, never boto3
directly. `db_gate.py` is the only entry point domain writes go through —
see its module docstring.
"""
