"""Pure data for the Blessings system setting.

Provenance (keep this current when entries change):

* NPC names, flows and blessing effect ids come from the legacy aC library
  (``Sources/aC_Scripts/aC_api/Blessing_Core.py`` and ``Verify_Blessing.py``) and the gwamm
  vanquish engine (``Sources/gwamm/runtime/game.py``).
* Model ids: gwamm found every legacy giver model id 51 lower than in the current game, so the
  ids below are the legacy ids + 51. Only the Purifier Krewe Member (6807) and Beacon of Droknar
  (5916) were taken from a working route; the rest are inferred. Givers are matched by name first
  and by model id only as a fallback, because names survive game updates and model ids do not.
* Dialog ids per campaign follow ``PyQuishAI_BT._blessing_parameters`` (Nightfall 0x85,
  Factions 0x86, Eye of the North 0x84). The rule for when a Kurzick/Luxon priest counts as the
  opposite faction mirrors ``ApoBottingLib.wrappers.TakeBlessing``. The opposite-faction
  conversation (0x81 "Let's talk this over", then 0x1 fight or 0x2 bribe) was read in game from a
  Kurzick Priest on 2026-10-12; the Luxon Priest is assumed to match.
* Ugar Mandoffson (dialog 0x84, standard flow) was supplied by the user on 2026-10-13 for a
  character high enough rank, or who has already beaten him, to skip his fight. His model id 6425
  was read in game through the bridge on 2026-10-13; it is the same model as the Norn Hunter.
* Map lists are the maps whose PyQuishAI vanquish data records a blessing point
  (``Sources/aC_Scripts/PyQuishAI_maps``), plus Verdant Cascades from the EotN storyline route.
  They are reference only: the service finds givers by scanning, on any map.
"""

from dataclasses import dataclass
from dataclasses import field
from enum import IntEnum


class Campaign(IntEnum):
    FACTIONS = 0
    NIGHTFALL = 1
    EYE_OF_THE_NORTH = 2


CAMPAIGN_TITLES: dict[Campaign, str] = {
    Campaign.FACTIONS: "Factions",
    Campaign.NIGHTFALL: "Nightfall",
    Campaign.EYE_OF_THE_NORTH: "Eye of the North",
}


class BlessingFlow(IntEnum):
    STANDARD = 0  # talk, send one dialog
    FACTION_PRIEST = 1  # optional bribe dialog first, then the blessing dialog
    NORN_CHALLENGE = 2  # talk, beat the hunter in a fight, talk again


class CombatMode(IntEnum):
    PAUSE = 0
    ABORT = 1


class Priority(IntEnum):
    BLESSING_FIRST = 0
    MOVEMENT_FIRST = 1


class OpposingPriestChoice(IntEnum):
    BRIBE = 0
    FIGHT = 1


DIALOG_NIGHTFALL = 0x85
DIALOG_FACTIONS = 0x86
DIALOG_EYE_OF_THE_NORTH = 0x84
# Opposite-faction priest: page 1 opens page 2, which offers the fight or the bribe.
DIALOG_PRIEST_TALK_IT_OVER = 0x81
DIALOG_PRIEST_FIGHT = 0x1
DIALOG_PRIEST_BRIBE = 0x2


@dataclass(frozen=True)
class BlessingNpc:
    key: str
    name: str
    campaign: Campaign
    name_fragments: tuple[str, ...]
    model_ids: tuple[int, ...]
    dialog_id: int
    flow: BlessingFlow = BlessingFlow.STANDARD
    faction: str = ""
    default_enabled: bool = True
    note: str = ""


BLESSING_NPCS: tuple[BlessingNpc, ...] = (
    BlessingNpc(
        "kurzick_priest", "Kurzick Priest", Campaign.FACTIONS, ("kurzick priest",), (3451, 3477),
        DIALOG_FACTIONS, BlessingFlow.FACTION_PRIEST, faction="kurzick", default_enabled=False,
        note="If you side with the Luxons: bribe or fight (see below).",
    ),
    BlessingNpc(
        "luxon_priest", "Luxon Priest", Campaign.FACTIONS, ("luxon priest",), (3692,),
        DIALOG_FACTIONS, BlessingFlow.FACTION_PRIEST, faction="luxon", default_enabled=False,
        note="If you side with the Kurzicks: bribe or fight (see below).",
    ),
    BlessingNpc("sunspear_scout", "Sunspear Scout", Campaign.NIGHTFALL, ("sunspear scout",), (4829, 4827),
                DIALOG_NIGHTFALL),
    BlessingNpc("wandering_priest", "Wandering Priest", Campaign.NIGHTFALL, ("wandering priest",), (5435, 5434),
                DIALOG_NIGHTFALL),
    BlessingNpc("vabbian_scout", "Vabbian Scout", Campaign.NIGHTFALL, ("vabbian scout",), (5683,),
                DIALOG_NIGHTFALL),
    BlessingNpc("ghostly_scout", "Ghostly Scout", Campaign.NIGHTFALL, ("ghostly scout",), (5598, 5599),
                DIALOG_NIGHTFALL),
    BlessingNpc("ghostly_priest", "Ghostly Priest", Campaign.NIGHTFALL, ("ghostly priest",), (5666,),
                DIALOG_NIGHTFALL),
    BlessingNpc("whispers_informant", "Whispers Informant", Campaign.NIGHTFALL, ("whispers informant",),
                (5269, 5734), DIALOG_NIGHTFALL),
    BlessingNpc("forgotten_warden", "Forgotten Warden", Campaign.NIGHTFALL, ("forgotten warden",), (5053,),
                DIALOG_NIGHTFALL),
    BlessingNpc("beacon_of_droknar", "Beacon of Droknar", Campaign.EYE_OF_THE_NORTH, ("beacon",), (5916,),
                DIALOG_EYE_OF_THE_NORTH),
    BlessingNpc("ascalonian_refugee", "Ascalonian Refugee", Campaign.EYE_OF_THE_NORTH, ("ascalonian refugee",),
                (2037, 2038, 6095, 6096, 6094), DIALOG_EYE_OF_THE_NORTH),
    BlessingNpc("asuran_krewe", "Asuran Krewe", Campaign.EYE_OF_THE_NORTH, ("krewe",),
                (6806, 6807, 6826, 6830), DIALOG_EYE_OF_THE_NORTH),
    BlessingNpc(
        "norn_hunter", "Norn Hunter", Campaign.EYE_OF_THE_NORTH, ("norn hunter",), (6425, 6431),
        DIALOG_EYE_OF_THE_NORTH, BlessingFlow.NORN_CHALLENGE, default_enabled=False,
        note="Turns hostile; you must win a fight before the blessing.",
    ),
    # Shares model 6425 with the Norn Hunter, which stays first so the model-id fallback keeps
    # the fight flow; the name match picks Ugar.
    BlessingNpc("ugar_mandoffson", "Ugar Mandoffson", Campaign.EYE_OF_THE_NORTH, ("mandoffson",), (6425,),
                DIALOG_EYE_OF_THE_NORTH, note="Gives the blessing directly once you outrank or have beaten him."),
)


@dataclass(frozen=True)
class BlessingRegion:
    campaign: Campaign
    name: str
    maps: tuple[tuple[int, str], ...]


BLESSING_REGIONS: tuple[BlessingRegion, ...] = (
    BlessingRegion(Campaign.FACTIONS, "Echovald Forest", ((201, "Melandru's Hope"),)),
    BlessingRegion(Campaign.NIGHTFALL, "Istan", (
        (432, "Cliffs of Dohjok"), (481, "Fahranur, The First City"), (486, "Issnur Isles"),
        (484, "Lahtenda Bog"), (488, "Mehtani Keys"), (430, "Plains of Jarin"), (483, "Zehlon Reach"),
    )),
    BlessingRegion(Campaign.NIGHTFALL, "Kourna", (
        (380, "Arkjok Ward"), (377, "Bahdok Caverns"), (375, "Barbarous Shore"), (379, "Dejarin Estate"),
        (382, "Gandara, the Moon Fortress"), (369, "Jahai Bluffs"), (371, "Marga Coast"),
        (373, "Sunward Marches"), (384, "The Floodplain of Mahnkelon"), (386, "Turai's Procession"),
    )),
    BlessingRegion(Campaign.NIGHTFALL, "Vabbi", (
        (399, "Forum Highlands"), (394, "Garden of Seborhin"), (395, "Holdings of Chokhin"),
        (402, "Resplendent Makuun"), (413, "The Hidden City of Ahdashim"), (419, "The Mirror of Lyss"),
        (397, "Vehjin Mines"), (406, "Vehtendi Valley"), (404, "Wilderness of Bahdza"),
        (392, "Yatendi Canyons"),
    )),
    BlessingRegion(Campaign.NIGHTFALL, "Desolation", (
        (448, "Crystal Overlook"), (443, "Poisoned Outcrops"), (446, "The Alkali Pan"),
        (439, "The Ruptured Heart"), (441, "The Shattered Ravines"), (444, "The Sulfurous Wastes"),
    )),
    BlessingRegion(Campaign.EYE_OF_THE_NORTH, "Charr Homelands", ((647, "Dalada Uplands"),)),
    BlessingRegion(Campaign.EYE_OF_THE_NORTH, "Far Shiverpeaks", (
        (482, "Bjora Marches"), (513, "Drakkar Lake"), (546, "Jaga Moraine"), (548, "Norrhart Domains"),
    )),
    BlessingRegion(Campaign.EYE_OF_THE_NORTH, "Tarnished Coast", (
        (572, "Alcazia Tangle"), (569, "Magus Stones"), (566, "Verdant Cascades"),
    )),
)


# Skill ids of every blessing / bounty effect; any one of them active means "already blessed".
BLESSING_EFFECT_IDS: frozenset[int] = frozenset((
    # Nightfall: Sunspear bounties
    1794, 1842, 1971, 1972, 1853, 1963, 1964, 1837, 1838, 1977, 1978, 1839, 1843, 1979, 1980,
    1791, 1840, 1841, 1852, 1961, 1962, 1832, 1969, 1970, 1822, 1823, 1824, 1825, 1850, 1959,
    1960, 2043, 2044, 1795, 1833, 1834, 1973, 1974, 1790, 1835, 1836, 1975, 1976, 1792, 1828,
    1965, 1966, 1796, 1854, 1981, 1982, 1967,
    # Nightfall: Lightbringer bounties
    1898, 2040, 1831, 1844, 2030, 2031, 1826, 1827, 1846, 2032, 2033, 1849, 2036, 2037, 1845,
    2038, 2039, 1847, 1848, 2034, 2035, 1851, 2041, 2042,
    # Factions
    593, 912, 1947, 1946,
    # Eye of the North
    2445, 2446, 2447, 2448, 2549, 2565, 2566, 2567, 2568, 2457, 2458, 2459, 2460, 2550, 2578,
    2434, 2435, 2436, 2481, 2548, 2552, 2469, 2470, 2471, 2472, 2551, 2591, 2592, 2593, 2594,
))


DETOUR_RANGE_MIN = 500
DETOUR_RANGE_MAX = 5000


@dataclass
class BlessingsConfig:
    enabled: bool = False
    detour_range: int = 2500
    combat_mode: CombatMode = CombatMode.PAUSE
    priority: Priority = Priority.BLESSING_FIRST
    opposing_priest: OpposingPriestChoice = OpposingPriestChoice.BRIBE
    npc_enabled: dict[str, bool] = field(default_factory=dict)

    def is_npc_enabled(self, npc: BlessingNpc) -> bool:
        return bool(self.npc_enabled.get(npc.key, npc.default_enabled))
