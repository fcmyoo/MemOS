"""
MemOS CLI Tool
This script provides command-line interface for MemOS operations.
"""

import argparse
import json
import os
import zipfile

from io import BytesIO


def get_openapi_app():
    """Return the FastAPI app used for OpenAPI export."""
    from memos.api.server_api import app

    return app


def export_openapi(output: str) -> bool:
    """Export OpenAPI schema to JSON file."""
    app = get_openapi_app()

    # Create directory if it doesn't exist
    if os.path.dirname(output):
        os.makedirs(os.path.dirname(output), exist_ok=True)

    with open(output, "w") as f:
        json.dump(app.openapi(), f, indent=2)
        f.write("\n")

    print(f"✅ OpenAPI schema exported to: {output}")
    return True


def download_examples(dest: str) -> bool:
    import requests

    """Download examples from the MemOS repository."""
    zip_url = "https://github.com/MemTensor/MemOS/archive/refs/heads/main.zip"
    print(f"📥 Downloading examples from {zip_url}...")

    try:
        response = requests.get(zip_url)
        response.raise_for_status()

        with zipfile.ZipFile(BytesIO(response.content)) as z:
            extracted_files = []
            for file in z.namelist():
                if "MemOS-main/examples/" in file and not file.endswith("/"):
                    # Remove the prefix and extract to dest
                    relative_path = file.replace("MemOS-main/examples/", "")
                    extract_path = os.path.join(dest, relative_path)

                    # Create directory if it doesn't exist
                    os.makedirs(os.path.dirname(extract_path), exist_ok=True)

                    # Extract the file
                    with z.open(file) as source, open(extract_path, "wb") as target:
                        target.write(source.read())
                    extracted_files.append(extract_path)

        print(f"✅ Examples downloaded to: {dest}")
        print(f"📁 {len(extracted_files)} files extracted")

    except requests.RequestException as e:
        print(f"❌ Error downloading examples: {e}")
        return False
    except Exception as e:
        print(f"❌ Error extracting examples: {e}")
        return False

    return True


def set_root_password(password: str) -> bool:
    """Set the root user's Web console password (Argon2id, via PostgreSQL)."""
    from memos.api.web_auth import WebPasswordService
    from memos.mem_user.factory import create_runtime_user_manager

    if len(password) < 6 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        print("❌ Password must be at least 6 characters and contain both letters and digits.")
        return False

    user_manager = create_runtime_user_manager()
    try:
        user = user_manager.get_user_by_name("root")
        if user is None:
            print("❌ Root user 'root' not found.")
            return False

        password_hash = WebPasswordService().hash_password(password)
        if not user_manager.set_user_password(user.user_id, password_hash):
            print("❌ Failed to set root password.")
            return False
    finally:
        user_manager.close()

    print("✅ Root password set successfully.")
    return True


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="memos",
        description="MemOS Command Line Interface",
    )

    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Download examples command
    examples_parser = subparsers.add_parser("download_examples", help="Download example files")
    examples_parser.add_argument(
        "--dest",
        type=str,
        default="./examples",
        help="Destination directory for examples (default: ./examples)",
    )

    # Export API command
    api_parser = subparsers.add_parser("export_openapi", help="Export OpenAPI schema to JSON file")
    api_parser.add_argument(
        "--output",
        type=str,
        default="openapi.json",
        help="Output path for OpenAPI schema (default: openapi.json)",
    )

    # Set root password command
    root_pwd_parser = subparsers.add_parser(
        "set_root_password", help="Set the root user's Web console password"
    )
    root_pwd_parser.add_argument(
        "--password",
        type=str,
        required=True,
        help="New password for the root user (minimum 6 characters, letters + digits)",
    )

    # Parse arguments
    args = parser.parse_args()

    # Handle commands
    if args.command == "download_examples":
        success = download_examples(args.dest)
        exit(0 if success else 1)
    elif args.command == "export_openapi":
        success = export_openapi(args.output)
        exit(0 if success else 1)
    elif args.command == "set_root_password":
        success = set_root_password(args.password)
        exit(0 if success else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
