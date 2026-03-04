"""Generate MemOS master key and hash for AUTH bootstrap."""

import argparse
from pathlib import Path

from memos.api.utils.api_keys import generate_master_key


def main() -> None:
    """Generate a master key and optionally append hash into an env file."""
    parser = argparse.ArgumentParser(description="Generate MemOS master key")
    parser.add_argument(
        "--output-env",
        type=str,
        default=None,
        help="Optional .env file path to append MASTER_KEY_HASH line",
    )
    args = parser.parse_args()

    key, key_hash = generate_master_key()
    env_line = f"MASTER_KEY_HASH={key_hash}"

    print("=" * 72)
    print("MemOS Master Key Generated")
    print("Please store this master key securely. It is shown only once.")
    print("=" * 72)
    print(f"MASTER_KEY: {key}")
    print("=" * 72)
    print("Copy the following line into your .env:")
    print(env_line)

    if args.output_env:
        output_path = Path(args.output_env)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as env_file:
            env_file.write(f"\n{env_line}\n")
        print(f"Appended MASTER_KEY_HASH to: {output_path}")


if __name__ == "__main__":
    main()
