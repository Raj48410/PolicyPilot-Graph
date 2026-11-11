"""Demo group-name mapping; use immutable group IDs for production authorization."""
from collections.abc import Iterable

def resolve_user_role(groups: Iterable[dict | str]) -> str:
    names = {group if isinstance(group, str) else group.get("displayName") for group in groups}
    for group_name, role in (("Global Admin", "Admin"), ("GlobalAdmin", "Admin"),
                             ("HR Team", "HR"), ("Finance Team", "Finance"), ("IT Team", "IT")):
        if group_name in names:
            return role
    return "Employee"
