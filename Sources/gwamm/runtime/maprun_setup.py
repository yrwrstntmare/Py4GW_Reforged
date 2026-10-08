"""Getting ready for one area in its outpost: secondary profession, team and hero bars,
signets on the bar, buying signets, and the checks for a party shared with other players."""
import json
import os
import time

import PySystem
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import builds as teambuilds
from ..core import elites
from . import game

EYE_OF_THE_NORTH = 642
SKILL_TRAINER_XY = (-3551.0, 2341.0)      # as used by Reforged's elite capture helper
SKILL_TRAINER_DIALOG = 0x84


class SetupMixin:
    def _signets_wanted(self):
        """How many signets to carry into this area: one per elite worth taking, up to the limit.
        With no boss list for the area, a small default so a lucky boss is not wasted."""
        c, cfg = self.campaign, self.session.cfg
        if not cfg.capture_elites:
            return 0
        area = elites.AREAS.get(self.map_id)
        if area is None:
            return min(c.max_signets, c.nodata_signets)
        primary, secondary = game.player_professions()
        learnt = {e.skill_id for e in area if game.skill_learnt(e.skill_id)}
        return min(c.max_signets, len(elites.prioritise(primary, secondary, area, learnt, self.map_id)))

    def _bar_for(self, want):
        c = self.campaign
        sec = game.player_professions()[1]          # the secondary we are on now (possibly just changed)
        slots, source = teambuilds.detect_signet_slots(c.builds, game.max_party_size(), c.signet_slots)
        self.session.log.event("campaign", map_id=self.map_id, phase="signets",
                               note=f"fallback bar: signets go in slots {slots}"
                                    + (f" (as in '{source}')" if source else " (no signet build saved, default slots)"))
        return game.bar_with_signets(c.capture_template, want, slots, sec) or c.capture_template

    # ---- secondary profession ----
    def _secondary_tree(self):
        """In the outpost: switch to the secondary that gets the best elites here, if that is not
        the one we are on. Returns None when no change is needed or possible."""
        c, cfg, log = self.campaign, self.session.cfg, self.session.log
        area = elites.AREAS.get(self.map_id)
        if not (cfg.capture_elites and cfg.change_secondary and area and c.max_signets > 0):
            return None
        primary, current = game.player_professions()
        unlocked = game.unlocked_secondaries()
        if not unlocked:
            log.event("campaign", map_id=self.map_id, phase="secondary", note="could not read unlocked professions")
            return None
        learnt = {e.skill_id for e in area if game.skill_learnt(e.skill_id)}
        try:
            done = game.vanquished_ids()
        except Exception:
            done = set()
        best, take = elites.best_secondary(primary, current, area, learnt, unlocked, c.max_signets, self.map_id, done)
        log.event("campaign", map_id=self.map_id, phase="secondary", current=current, best=best,
                  unlocked=sorted(unlocked), would_take=[e.name for e in take])
        if best == current or not best:
            return None
        self._restore_secondary = True

        def by_template():
            t = game.template_with_secondary(c.capture_template or game.bar_template(), best)
            if t:
                game.load_bar_template(t)

        def by_call_if_needed():
            if game.player_professions()[1] != best:
                game.change_secondary_direct(best)

        def report():
            now = game.player_professions()[1]
            log.event("campaign", map_id=self.map_id, phase="secondary", wanted=best, now=now, changed=(now == best))

        return BT.Sequence(name=f"Secondary:{self.map_id}", children=[
            self._action("Change secondary (load bar)", by_template), BT.Wait(duration_ms=2000),
            self._action("Change secondary (direct)", by_call_if_needed), BT.Wait(duration_ms=2000),
            self._action("Check secondary", report)])

    def _after_secondary(self):
        """Secondary settled: put the right team and bar on for this area, then head out."""
        c = self.campaign
        want = self._signets_wanted()
        team, carried = c.team_for(game.max_party_size(), want)
        if self.session.refresh_party():
            # Other players are in the party: nobody is dismissed or added. Your own heroes still
            # get their saved bars, the other accounts are checked, then your bar is set.
            self._team = None
            steps = self._shared_party_steps(team)
            if want > 0 and c.capture_template:
                steps.append(self._signet_tree(want))
            elif c.capture_template and self.session.cfg.capture_elites:
                steps.append(self._action("Your bar", lambda: game.load_bar_template(c.capture_template)))
            self._set("signets", BT.Sequence(name=f"SharedParty:{self.map_id}", children=steps + [BT.Wait(duration_ms=300)]))
            return
        self._team = team
        if team is not None:
            self._set("signets", self._team_tree(team, carried))
        elif want > 0 and c.capture_template:
            self._set("signets", self._signet_tree(want))          # no saved team: swap signets onto your bar
        else:
            if c.capture_template and self.session.cfg.capture_elites:
                game.load_bar_template(c.capture_template)         # nothing to capture here: full bar
            self._next_leg()

    def _shared_party_steps(self, team):
        """Party with other players in it. (1) Each hero of yours gets its bar from the team saved
        for this party size, or failing that from any saved team that has that hero. (2) The other
        accounts are looked up in Py4GW's shared memory: HeroAI following and fighting are
        switched on where they are off, and players Py4GW is not running for are reported."""
        c, log, steps = self.campaign, self.session.log, []
        saved = {}
        for d in c.builds.values():
            try:
                for hero_id, template, _b in teambuilds.Team.from_dict(d).heroes:
                    if template:
                        saved.setdefault(int(hero_id), template)
            except Exception:
                continue
        if team is not None:
            for hero_id, template, _b in team.heroes:
                if template:
                    saved[int(hero_id)] = template           # this party size's team wins
        mine, loaded = game.my_hero_ids(), []
        for pos, hero_id in enumerate(mine, start=1):
            template = saved.get(hero_id)
            if template:
                loaded.append(hero_id)
                steps += [self._action(f"Hero {pos} bar", lambda p=pos, t=template: game.load_hero_template(p, t)),
                          BT.Wait(duration_ms=350)]

        def check():
            accounts = game.party_accounts()
            others = max(0, self.session.party.get("players", 1) - 1)
            fixed = []
            for a in accounts:
                for opt, key in (("Following", "following"), ("Combat", "combat")):
                    if a[key] is False:
                        game.set_heroai_option(a["email"], opt, True)
                        fixed.append(f"{a['name']}: {opt} on")
            missing = others - len(accounts)
            self.session.party_note = (
                f"{len(accounts)} of {others} other player(s) run Py4GW with HeroAI"
                + (f"; {missing} do not, the bot cannot steer them" if missing > 0 else "")
                + (f"; switched on: {', '.join(fixed)}" if fixed else ""))
            log.event("campaign", map_id=self.map_id, phase="team", shared_party=True, makeup=self.session.party,
                      my_heroes=mine, hero_bars_loaded=loaded, accounts=[a["name"] for a in accounts],
                      not_controlled=max(0, missing), switched_on=fixed)
        steps.append(self._action("Check the other accounts", check))
        return steps

    def _team_tree(self, team, signets):
        """Load a saved team: heroes, their bars, your bar. Then buy signets if the bar is short."""
        log = self.session.log
        # Where the secondary was chosen for this area's elites, keep it: a saved bar made as, say,
        # E/R must not quietly switch the secondary back.
        chosen = self.session.cfg.change_secondary and elites.AREAS.get(self.map_id) is not None
        sec = game.player_professions()[1] if chosen else None
        bar = (game.template_with_secondary(team.player, sec) if sec else "") or team.player
        steps = []
        wanted_heroes = [h[0] for h in team.heroes]
        if wanted_heroes and game.party_hero_ids() != wanted_heroes:
            steps += [self._action("Dismiss heroes", game.kick_all_heroes), BT.Wait(duration_ms=1000)]
            for hero_id in wanted_heroes:
                steps += [self._action(f"Add hero {hero_id}", lambda h=hero_id: game.add_hero(h)), BT.Wait(duration_ms=700)]
        # The game does not always seat heroes in the order they were added, so each bar goes to
        # wherever its hero actually sits now, never to the place it has in the saved team.
        def hero_bar(hero_id, template, fallback):
            now = game.party_hero_ids()
            if hero_id in now:
                game.load_hero_template(now.index(hero_id) + 1, template)
            elif not now:
                game.load_hero_template(fallback, template)      # party unreadable: saved order
            else:
                log.event("campaign", map_id=self.map_id, phase="team", note=f"hero {hero_id} not in party; bar not loaded")
        for pos, (hero_id, template, _behaviour) in enumerate(team.heroes, start=1):
            if template:
                steps += [self._action(f"Hero {hero_id} bar", lambda h=hero_id, t=template, p=pos: hero_bar(h, t, p)),
                          BT.Wait(duration_ms=350)]
        steps += [self._action("Your bar", lambda: game.load_bar_template(bar)), BT.Wait(duration_ms=2000),
                  self._action("Report team", lambda: log.event(
                      "campaign", map_id=self.map_id, phase="team", team=team.name, heroes_wanted=wanted_heroes,
                      heroes_now=game.party_hero_ids(), signets_on_bar=game.capture_signets(), signets_wanted=signets))]
        if signets > 0:
            steps.append(BehaviorTree.SubtreeNode(name="TopUpSignets", subtree_fn=lambda _n: self._buy_tree(bar, signets)))
        return BT.Sequence(name=f"Team:{self.map_id}", children=steps)

    def _to_trainer(self):
        """Walk to the skill trainer and open the shop. Our own route, in short hops, then
        Reforged's walk-and-talk for the last step (see geometry.town_waypoints for why)."""
        log = self.session.log

        def build(_node):
            hops = []
            try:
                from ..core.geometry import town_waypoints
                plane, _z = game.player_level()
                hops = town_waypoints(game.read_trapezoids(), game.read_level_links(), game.player_xy(),
                                      SKILL_TRAINER_XY, start_plane=plane)
            except Exception as e:
                log.event("campaign", map_id=self.map_id, phase="signets", note=f"town route unreadable: {e!r}")
            self.session.errand_note = f"skill trainer: {len(hops)} hops" if hops else "skill trainer: straight there (no route found)"
            walk = [BT.Move(pos=(float(x), float(y)), tolerance=180.0) for x, y in hops[:-1]]
            return BT.Sequence(name="ToTrainer", children=walk + [
                BT.MoveAndDialog(pos=SKILL_TRAINER_XY, dialog_id=SKILL_TRAINER_DIALOG)])
        return BehaviorTree.SubtreeNode(name="ToTrainer", subtree_fn=build)

    def _buy_tree(self, bar, want):
        """Buy the signets the bar is short of at the Eye of the North, come back, reload the bar."""
        log = self.session.log
        short = want - game.capture_signets()
        if short <= 0 or not self.session.cfg.buy_signets:
            return BT.Wait(duration_ms=100)
        if not game.map_unlocked(EYE_OF_THE_NORTH):
            log.event("campaign", map_id=self.map_id, phase="signets", note="Eye of the North not unlocked; cannot buy")
            return BT.Wait(duration_ms=100)
        buy = []
        for _ in range(short):
            buy += [self._action("Buy Signet of Capture", game.buy_signet), BT.Wait(duration_ms=1200)]
        return BT.Sequence(name="BuySignets", children=[
            self._always(BT.Sequence(name="Shop", children=[
                BT.Travel(target_map_id=EYE_OF_THE_NORTH), BT.Wait(duration_ms=2000),
                self._to_trainer(), BT.Wait(duration_ms=800),
                *buy]), "buying signets", limit_s=150.0),
            BT.Travel(target_map_id=self._home, hard_mode=True), BT.Wait(duration_ms=2000),
            self._action("Your bar", lambda: game.load_bar_template(bar)), BT.Wait(duration_ms=2000),
            self._action("Count signets", lambda: log.event("campaign", map_id=self.map_id, phase="signets",
                                                             have=game.capture_signets(), want=want, after_buying=True)),
        ])

    def _signet_tree(self, want):
        """Load the capture bar; if it comes up short of signets, buy the difference from the
        skill trainer at the Eye of the North and come back."""
        c, log = self.campaign, self.session.log
        bar = self._bar_for(want)
        steps = [self._action("Put signets on the bar", lambda: game.load_bar_template(bar)),
                 BT.Wait(duration_ms=2000)]

        def shop(_node):
            have = game.capture_signets()
            log.event("campaign", map_id=self.map_id, phase="signets", have=have, want=want)
            short = want - have
            if short <= 0 or not self.session.cfg.buy_signets:
                return BT.Wait(duration_ms=100)
            if not game.map_unlocked(EYE_OF_THE_NORTH):
                log.event("campaign", map_id=self.map_id, phase="signets", note="Eye of the North not unlocked; cannot buy")
                return BT.Wait(duration_ms=100)
            buy = []
            for _ in range(short):
                buy += [self._action("Buy Signet of Capture", game.buy_signet), BT.Wait(duration_ms=1200)]
            return BT.Sequence(name="BuySignets", children=[
                self._always(BT.Sequence(name="Shop", children=[
                    BT.Travel(target_map_id=EYE_OF_THE_NORTH),
                    BT.Wait(duration_ms=2000),
                    self._to_trainer(),
                    BT.Wait(duration_ms=800),
                    *buy]), "buying signets", limit_s=150.0),
                BT.Travel(target_map_id=self._home, hard_mode=True),
                BT.Wait(duration_ms=2000),
                self._action("Put signets on the bar", lambda: game.load_bar_template(bar)),
                BT.Wait(duration_ms=2000),
                self._action("Count signets", lambda: log.event("campaign", map_id=self.map_id, phase="signets",
                                                                 have=game.capture_signets(), want=want, after_buying=True)),
            ])

        steps.append(BehaviorTree.SubtreeNode(name="TopUpSignets", subtree_fn=shop))
        return BT.Sequence(name=f"Signets:{self.map_id}", children=steps)
