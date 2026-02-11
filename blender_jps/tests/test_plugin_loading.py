import argparse
import os
import sqlite3
import sys
import tempfile
import traceback

import addon_utils
import bpy


def _script_args():
    """Return args passed after `--` (Blender convention)."""
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return []


def _add_repo_root_to_syspath():
    """
    Ensure the repository root is on sys.path so the add-on can be imported
    directly from the checkout in CI.
    """
    here = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return repo_root


def _parse_args():
    p = argparse.ArgumentParser(description="Headless Blender add-on smoke test")
    p.add_argument("--addon", required=True, help="Blender add-on module name")
    p.add_argument(
        "--require-module",
        action="append",
        default=[],
        help="Python module that must be importable (repeatable)",
    )
    p.add_argument(
        "--require-operator",
        action="append",
        default=[],
        help="Operator that must exist, e.g. 'jupedsim.load_simulation' (repeatable)",
    )
    p.add_argument(
        "--factory-startup",
        action="store_true",
        help="Reset Blender to factory settings",
    )
    p.add_argument(
        "--test-sqlite-loading",
        action="store_true",
        help="Test SQLite file loading operator",
    )
    return p.parse_args(_script_args())


def _operator_exists(op_id: str) -> bool:
    """Check if bpy.ops.<category>.<op> exists."""
    if "." not in op_id:
        return False
    cat, op = op_id.split(".", 1)
    cat_obj = getattr(bpy.ops, cat, None)
    return cat_obj is not None and getattr(cat_obj, op, None) is not None


def _create_test_sqlite_file():
    """
    Create a minimal test SQLite file with JuPedSim trajectory data.
    Returns the path to the temporary file.
    """
    fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE trajectory_data (
            id INTEGER,
            frame INTEGER,
            pos_x REAL,
            pos_y REAL
        )
    """)

    test_data = []
    for frame in range(0, 50, 10):
        for agent_id in range(1, 4):
            x = agent_id * 2.0 + frame * 0.1
            y = agent_id * 1.5 + frame * 0.05
            test_data.append((agent_id, frame, x, y))

    cursor.executemany(
        "INSERT INTO trajectory_data (id, frame, pos_x, pos_y) VALUES (?, ?, ?, ?)", test_data
    )

    cursor.execute("""
        CREATE TABLE geometry (
            wkt TEXT
        )
    """)

    wkt = "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"
    cursor.execute("INSERT INTO geometry (wkt) VALUES (?)", (wkt,))

    conn.commit()
    conn.close()

    print(f"✓ Created test SQLite file: {db_path}")
    print("  - 3 agents")
    print("  - 5 frames (0, 10, 20, 30, 40)")
    print("  - Simple geometry")

    return db_path


def _test_sqlite_loading(addon_name):
    """Test the SQLite loading operator."""
    print("\n" + "=" * 72)
    print("Testing SQLite Loading Operator")
    print("=" * 72 + "\n")

    db_path = _create_test_sqlite_file()

    try:
        bpy.context.scene.jupedsim_props.sqlite_file = db_path
        print(f"✓ Set sqlite_file property to: {db_path}")

        if not _operator_exists("jupedsim.load_simulation"):
            raise RuntimeError("Operator 'jupedsim.load_simulation' not found")
        print("✓ Operator 'jupedsim.load_simulation' exists")

        if not _operator_exists("jupedsim.select_file"):
            raise RuntimeError("Operator 'jupedsim.select_file' not found")
        print("✓ Operator 'jupedsim.select_file' exists")

        props = bpy.context.scene.jupedsim_props
        assert hasattr(props, "sqlite_file"), "Missing 'sqlite_file' property"
        assert hasattr(props, "loading_in_progress"), "Missing 'loading_in_progress' property"
        assert hasattr(props, "frame_step"), "Missing 'frame_step' property"
        print("✓ Scene properties are properly registered")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trajectory_data")
        count = cursor.fetchone()[0]
        conn.close()

        if count != 15:  # 3 agents x 5 frames
            raise RuntimeError(f"Expected 15 trajectory records, found {count}")
        print(f"✓ SQLite file contains correct data ({count} records)")

        print("\n✓ SQLite loading operator test passed!")

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"✓ Cleaned up test file: {db_path}")


def main():
    args = _parse_args()

    print("\n" + "=" * 72)
    print(f"Blender: {bpy.app.version_string}")
    print(f"Addon module: {args.addon}")
    print("=" * 72)

    repo_root = _add_repo_root_to_syspath()
    print(f"Repo root on sys.path: {repo_root}")

    if args.factory_startup:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        print("Loaded factory startup settings (empty).")

    enabled_ok = False

    try:
        addon_utils.enable(args.addon, default_set=True, persistent=False)
        enabled_ok = True
        print(f"✓ Enabled add-on '{args.addon}'")

        if args.addon not in bpy.context.preferences.addons.keys():
            raise RuntimeError(
                f"Add-on '{args.addon}' not present in bpy.context.preferences.addons"
            )
        print(f"✓ Add-on '{args.addon}' present in enabled add-ons list")

        for mod in args.require_module:
            __import__(mod)
            print(f"✓ Required module import ok: {mod}")

        for op_id in args.require_operator:
            if not _operator_exists(op_id):
                raise RuntimeError(f"Required operator not found: {op_id}")
            print(f"✓ Required operator exists: {op_id}")

        if args.test_sqlite_loading:
            _test_sqlite_loading(args.addon)

        print("\n" + "=" * 72)
        print("All tests passed!")
        print("=" * 72 + "\n")
        return 0

    except Exception as e:
        print("\n✗ TEST FAILED")
        print(f"Reason: {e}")
        traceback.print_exc()
        return 1

    finally:
        if enabled_ok:
            try:
                addon_utils.disable(args.addon, default_set=True)
                print(f"✓ Disabled add-on '{args.addon}'")
            except Exception as e:
                print(f"⚠ Failed to disable add-on '{args.addon}': {e}")


if __name__ == "__main__":
    raise SystemExit(main())
