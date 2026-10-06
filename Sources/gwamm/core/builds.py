"""Saved team builds: which bar and heroes to use for a given party size and number of
Signets of Capture. Pure Python. Reads GWToolbox++'s herobuilds.ini directly."""
import re

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
SIGNET_OF_CAPTURE = 3
SIZES = (8, 6, 4)

# GWToolbox++ stores a hero as an index into this list (its HeroIndexToID table), not as the
# game's hero id. Values are the game's hero ids.
TOOLBOX_HERO_IDS = [0, 2, 6, 18, 5, 12, 19, 3, 7, 27, 4, 14, 21, 1, 15, 24, 8, 10, 26, 13, 20, 36, 25, 37,
                    11, 17, 22, 9, 16, 23, 28, 29, 30, 31, 32, 33, 34, 35, 38, 39]


def decode_template(template):
    """(primary, secondary, [8 skill ids]) from a skill template code, or None if unreadable."""
    try:
        bits = []
        for ch in template.strip():
            v = _B64.index(ch)
            bits += [(v >> i) & 1 for i in range(6)]
        pos = [0]

        def read(n):
            v = sum(bits[pos[0] + i] << i for i in range(n) if pos[0] + i < len(bits))
            pos[0] += n
            return v

        if read(4) == 14:
            read(4)
        else:
            pos[0] = 0
        pb = read(2) * 2 + 4
        primary, secondary = read(pb), read(pb)
        count, ab = read(4), read(4) + 4
        for _ in range(count):
            read(ab)
            read(4)
        sb = read(4) + 8
        return primary, secondary, [read(sb) for _ in range(8)]
    except Exception:
        return None


class Team:
    def __init__(self, name, player, heroes):
        self.name, self.player, self.heroes = name, player, heroes     # heroes: [[hero id, template, behaviour]]
        d = decode_template(player) if player else None
        self.primary = d[0] if d else 0
        self.secondary = d[1] if d else 0
        self.signets = d[2].count(SIGNET_OF_CAPTURE) if d else 0

    @property
    def size(self):
        """Party size this team is for: the standard size it fits (8, 6 or 4)."""
        n = len(self.heroes) + 1
        return next((s for s in reversed(SIZES) if n <= s), 8)

    def to_dict(self):
        return {"name": self.name, "player": self.player, "heroes": self.heroes}

    @staticmethod
    def from_dict(d):
        return Team(d.get("name", ""), d.get("player", ""), [list(h) for h in d.get("heroes", [])])


def parse_toolbox_herobuilds(text):
    """Teams from GWToolbox++'s herobuilds.ini."""
    teams = []
    for section in re.split(r"^\[builds\d+\]\s*$", text, flags=re.M)[1:]:
        kv = dict(re.findall(r"^(\w+)[ \t]*=[ \t]*([^\r\n]*?)[ \t]*\r?$", section, flags=re.M))
        player, heroes = kv.get("template0", ""), []
        for i in range(1, 8):
            template = kv.get(f"template{i}", "")
            try:
                index = int(kv.get(f"heroindex{i}", "0"))
                behaviour = int(kv.get(f"behavior{i}", "1"))
            except ValueError:
                continue
            if 0 < index < len(TOOLBOX_HERO_IDS) and template and set(template[1:]) != {"A"}:
                heroes.append([TOOLBOX_HERO_IDS[index], template, behaviour])
        if player and set(player[1:]) != {"A"}:
            teams.append(Team(kv.get("buildname", "") or "(unnamed)", player, heroes))
    return teams


def parse_toolbox_json(text):
    """Teams from GWToolbox++'s herobuilds.json (the format current Toolbox versions save, under
    configs/<profile>/). The first build of a team is the player's; hero_id is the game's own."""
    import json
    teams = []
    for t in json.loads(text).get("teambuilds", []):
        rows = t.get("builds", [])
        if not rows:
            continue
        player, heroes = rows[0].get("code", ""), []
        for b in rows[1:8]:
            code, hero = b.get("code", ""), int(b.get("hero_id", 0) or 0)
            if hero > 0 and code and set(code[1:]) != {"A"}:
                heroes.append([hero, code, int(b.get("behavior", 1) or 0)])
        if player and set(player[1:]) != {"A"}:
            teams.append(Team(t.get("name", "") or "(unnamed)", player, heroes))
    return teams


def parse_toolbox(text, path=""):
    if path.lower().endswith(".json") or text.lstrip().startswith("{"):
        return parse_toolbox_json(text)
    return parse_toolbox_herobuilds(text)


def slot_key(size, signets):
    return f"{size}:{signets}"


def auto_assign(teams, primary, slots):
    """Fill empty slots (party size x signet count) with the first imported team that fits this
    character. Teams whose bar is for another profession are ignored. Returns how many were set."""
    filled = 0
    for t in teams:
        if t.primary != primary:
            continue
        key = slot_key(t.size, min(2, t.signets))
        if key not in slots:
            slots[key] = t.to_dict()
            filled += 1
    return filled


def pick(slots, size, signets):
    """The team to use: the right party size, with as many signets as wanted or the nearest fewer.
    Returns (Team, signets it carries) or (None, 0)."""
    size = next((s for s in SIZES if size >= s), 4)
    for n in range(min(2, signets), -1, -1):
        d = slots.get(slot_key(size, n))
        if d:
            return Team.from_dict(d), n
    return None, 0


def signet_positions(template):
    """Bar slots (1-8) holding a Signet of Capture in a skill template."""
    d = decode_template(template) if template else None
    return [i + 1 for i, sk in enumerate(d[2]) if sk == SIGNET_OF_CAPTURE] if d else []


def detect_signet_slots(slots, size, default=(5, 6)):
    """Which bar slots to give up for signets, read from the saved builds: where the signet builds
    put them. Builds for this party size are looked at first, then the other sizes; a two-signet
    build gives both slots, a one-signet build the first. Returns (slots, name of the build used
    or "")."""
    size = next((s for s in SIZES if size >= s), 4)
    out, source = [], ""
    for sz in [size] + [s for s in SIZES if s != size]:
        for n in (1, 2):                      # the one-signet build decides which slot goes first
            d = slots.get(slot_key(sz, n))
            for pos in signet_positions(d.get("player", "")) if d else []:
                if pos not in out:
                    out.append(pos)
                    source = source or d.get("name", "")
        if len(out) >= 2:
            break
    for pos in default:
        if len(out) < 2 and pos not in out:
            out.append(pos)
    return out[:2], source
