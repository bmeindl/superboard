"""Install the optional, workspace-owned Crew starter."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def install_crew(root: Path) -> int:
    source = Path(__file__).with_name("crew_starter")
    destination = root / "crew"
    files = [path.relative_to(source) for path in source.rglob("*") if path.is_file()]
    # The manifest enables role routing. Make it visible only after its assets exist.
    manifest = Path("crew.json")
    if manifest not in files:
        raise OSError("the installed package is missing its Crew starter")
    files.remove(manifest)
    files = sorted(files) + [manifest]

    for relative in files:
        target = destination / relative
        for component in (target, *target.parents):
            if component == root:
                break
            if component.is_symlink():
                raise OSError(f"Crew starter path is a symlink: {component}")
            if component.exists():
                expected_type = component.is_file() if component == target else component.is_dir()
                if not expected_type:
                    raise OSError(f"Crew starter path has the wrong type: {component}")

    created = 0
    for relative in files:
        target = destination / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Linking the complete file is atomic and never replaces a concurrent edit.
        with NamedTemporaryFile(dir=target.parent) as temporary:
            temporary.write((source / relative).read_bytes())
            temporary.flush()
            try:
                os.link(temporary.name, target)
                created += 1
            except FileExistsError:
                pass
    return created
