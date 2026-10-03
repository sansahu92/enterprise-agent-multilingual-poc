"""Identity for the PoC.

PoC: personas stand in for Entra users; their groups stand in for the 'groups' claim of a validated token.
Production: groups come ONLY from the validated Entra ID token (or Graph), never from the prompt or the model.
"""
import hashlib
import re
from dataclasses import dataclass, field

GROUP_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    groups: tuple = field(default_factory=tuple)

    @property
    def pseudonym(self) -> str:
        """Stable pseudonymous ID for telemetry and analytics (no names in logs)."""
        return "u_" + hashlib.sha256(self.user_id.encode()).hexdigest()[:12]


PERSONAS = {
    "employee": User("emp01", "Employee (Aisha)", ("ALL_EMPLOYEES",)),
    "hr": User("hr01", "HR Officer (Omar)", ("ALL_EMPLOYEES", "HR")),
    "finance": User("fin01", "Finance Officer (Mariam)", ("ALL_EMPLOYEES", "FINANCE")),
    "admin": User("adm01", "Administrator (Khalid)", ("ALL_EMPLOYEES", "ADMIN")),
}


def get_user(persona: str) -> User:
    if persona not in PERSONAS:
        raise PermissionError(f"Unknown persona '{persona}'")
    return PERSONAS[persona]


def build_security_filter(groups) -> str:
    """Build the Azure AI Search OData security filter from the user's groups.

    Fail closed: no groups, or any malformed group name, raises instead of returning a broad filter.
    Superseded documents are always excluded.
    """
    groups = [g for g in (groups or [])]
    if not groups:
        raise PermissionError("No groups resolved for user — failing closed")
    for g in groups:
        if not GROUP_PATTERN.match(g):
            raise PermissionError(f"Invalid group name '{g}' — failing closed")
    joined = ",".join(groups)
    return f"allowed_groups/any(g: search.in(g, '{joined}', ',')) and is_current eq true"
