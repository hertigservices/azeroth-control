"""Build addons_catalog.json for the Azeroth Control addon manager.

Source: NoM0Re/WoW-3.3.5a-Addons (a curated, widely-used 3.3.5a addon collection).
Only .zip entries are listed - Python's stdlib cannot extract .rar, and shipping a
rar binary just for five packages isn't worth it. Those five are reported as skipped
rather than silently dropped.

Deliberately excluded: ZygorGuides and RestedXP backports. Both redistribute PAID
commercial guide products. QuestHelper / Carbonite / EveryQuest cover the same need
and are free.
"""
import json, os, sys, urllib.request

REPO = 'NoM0Re/WoW-3.3.5a-Addons'
API = 'https://api.github.com/repos/%s/contents/src/Addons' % REPO
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'addons_catalog.json')

# name -> (category, one-line description). Anything unlisted falls back to Utility.
CURATED = {
    # --- Interface ---
    'ElvUI':            ('Interface', 'Complete UI replacement: unit frames, bars, chat, bags, minimap'),
    'Bartender4':       ('Interface', 'Fully configurable action bars'),
    'Dominos':          ('Interface', 'Lightweight action bar replacement'),
    'ShadowedUnitFrames': ('Interface', 'Highly customisable player/target/raid frames'),
    'SexyMap':          ('Interface', 'Minimap styling and cleanup'),
    'Mapster':          ('Interface', 'World map improvements: scale, coords, fog'),
    'TipTac':           ('Interface', 'Tooltip styling, anchoring and extra info'),
    'BlizzMove':        ('Interface', 'Drag default Blizzard windows anywhere'),
    'SexyCooldown':     ('Interface', 'Cooldowns as a timeline bar'),
    'OPie':             ('Interface', 'Radial pop-up rings for spells and items'),
    'pMinimap':         ('Interface', 'Minimal minimap skin'),
    'CleanMinimap':     ('Interface', 'Strips minimap clutter'),
    'ActionBarSaver':   ('Interface', 'Save and restore action bar layouts'),
    'BindPad':          ('Interface', 'Keybind spells/items without using bars'),
    'SnowfallKeyPress': ('Interface', 'Fires actions on key down instead of key up'),
    # --- Combat ---
    'DBM':              ('Combat', 'Deadly Boss Mods: boss timers and warnings'),
    'Skada':            ('Combat', 'Damage and healing meter, modular'),
    'Details':          ('Combat', 'Detailed damage meter with deep breakdowns'),
    'Omen':             ('Combat', 'Threat meter'),
    'GTFO':             ('Combat', 'Audio alert when standing in damaging effects'),
    'xCT+':             ('Combat', 'Replacement scrolling combat text'),
    'MikScrollingBattleText': ('Combat', 'Classic scrolling combat text'),
    'TellMeWhen':       ('Combat', 'Configurable cooldown, proc and aura alerts'),
    'NeedToKnow':       ('Combat', 'Aura and cooldown timer bars'),
    'Critline':         ('Combat', 'Tracks your top hits and heals'),
    'InterruptBar':     ('Combat', 'Tracks enemy interrupt cooldowns'),
    'LoseControl':      ('Combat', 'Shows loss-of-control effects on you and targets'),
    'TrufiGCD':         ('Combat', 'Visual history of abilities used'),
    'EventHorizon':     ('Combat', 'Timeline view of DoTs and buffs'),
    'Afflicted':        ('Combat', 'Enemy cooldown and CC tracking'),
    'EnsidiaFails':     ('Combat', 'Announces raid mistakes'),
    'TidyPlates':       ('Combat', 'Enhanced, themeable nameplates'),
    # --- Healing ---
    'Grid2':            ('Healing', 'Compact raid frames with status indicators'),
    'HealBot':          ('Healing', 'Click-to-heal raid frames'),
    'VuhDo':            ('Healing', 'Advanced healing frames and click-casting'),
    'Decursive':        ('Healing', 'One-click debuff and curse removal'),
    'Clique':           ('Healing', 'Bind spells to mouse clicks on frames'),
    'RaidBuffStatus':   ('Healing', 'Shows which raid buffs are missing'),
    'PallyPower':       ('Healing', 'Paladin blessing assignment'),
    # --- Quests & Maps ---
    'QuestHelper':      ('Quests & Maps', 'Quest objective routing and map guidance (free Zygor alternative)'),
    'Carbonite':        ('Quests & Maps', 'Map overhaul with quest tracking and gathering routes'),
    'Cartographer':     ('Quests & Maps', 'Classic map suite: notes, herbalism, mining'),
    'EveryQuest':       ('Quests & Maps', 'Browse and track all quests in the game'),
    'TurnIn-2.1':       ('Quests & Maps', 'Auto-accepts and turns in quests'),
    'Cromulent':        ('Quests & Maps', 'Adds zone level ranges to the world map'),
    'Overachiever':     ('Quests & Maps', 'Achievement browsing and hints'),
    # --- Loot & Gear ---
    'AtlasLoot':        ('Loot & Gear', 'Full loot tables for dungeons and raids'),
    'Atlas':            ('Loot & Gear', 'Dungeon and raid maps with boss locations'),
    'GearScore':        ('Loot & Gear', 'Gear scoring for inspected players'),
    'GearScoreLite':    ('Loot & Gear', 'Lightweight gear scoring'),
    'InspectEquip':     ('Loot & Gear', 'Shows inspected players\' equipment in tooltips'),
    'Outfitter':        ('Loot & Gear', 'Gear set manager with auto-switching'),
    'MogIt':            ('Loot & Gear', 'Browse and preview item appearances'),
    'Ludwig':           ('Loot & Gear', 'Searchable database of every item'),
    'ClassLoot':        ('Loot & Gear', 'Filters loot tables to your class'),
    'RaidRoll':         ('Loot & Gear', 'Loot rolling and distribution helper'),
    'AllStats':         ('Loot & Gear', 'Expanded character stat display'),
    # --- Economy ---
    'Auctionator':      ('Economy', 'Fast auction house buying, selling and price history'),
    'AckisRecipeList':  ('Economy', 'Tracks which profession recipes you still need'),
    'Collectinator':    ('Economy', 'Tracks missing pets and mounts'),
    'Molinari':         ('Economy', 'One-click milling, prospecting and disenchanting'),
    'autorepair':       ('Economy', 'Repairs gear automatically at vendors'),
    # --- Bags & Mail ---
    'AdiBags':          ('Bags & Mail', 'Single-window bags with automatic categories'),
    'Altoholic':        ('Bags & Mail', 'Tracks all your characters\' inventory and professions'),
    'Postal':           ('Bags & Mail', 'Bulk mail sending and one-click inbox emptying'),
    'AmILockedOut':     ('Bags & Mail', 'Shows raid lockouts across your characters'),
    # --- Social ---
    'Chatter':          ('Social', 'Chat frame improvements and formatting'),
    'WIM':              ('Social', 'Whispers in separate instant-message windows'),
    'ChatFilter':       ('Social', 'Filters spam and unwanted chat'),
    'IgnoreMore':       ('Social', 'Raises the ignore list limit'),
    'spy':              ('Social', 'Warns when enemy players are detected nearby'),
    'AddFriend':        ('Social', 'Adds an Add Friend option to unit menus'),
    'FriendShare':      ('Social', 'Share friend lists between characters'),
    # --- PvP ---
    'GladiusEx':        ('PvP', 'Arena enemy frames'),
    'NotPlater-3.3.5':  ('PvP', 'Nameplate customisation aimed at PvP'),
    'RaidBrowser':      ('PvP', 'Browse and join raid groups'),
    'RaidComp':         ('PvP', 'Raid composition planner'),
    # --- Utility ---
    'ACP':              ('Utility', 'Addon Control Panel: enable/disable addons in-game'),
    'BugSack':          ('Utility', 'Collects Lua errors quietly for later review'),
    'SharedMedia':      ('Utility', 'Shared fonts, textures and sounds (library)'),
    'Talented':         ('Utility', 'Talent template planner and applier'),
    'SpellID':          ('Utility', 'Shows spell and item IDs in tooltips'),
    'Overachiever ':    ('Utility', 'Achievement helper'),
}

# The essentials, spanning every category - shown first so 111 entries stay approachable.
RECOMMENDED = {
    'ElvUI', 'Bartender4', 'ShadowedUnitFrames', 'TipTac', 'SexyMap',
    'DBM', 'Skada', 'Details', 'Omen', 'GTFO', 'TellMeWhen', 'TidyPlates',
    'Grid2', 'HealBot', 'Decursive', 'Clique',
    'QuestHelper', 'Carbonite', 'Cartographer',
    'AtlasLoot', 'Atlas', 'GearScore', 'Ludwig',
    'Auctionator', 'AckisRecipeList', 'Molinari',
    'AdiBags', 'Altoholic', 'Postal',
    'Chatter', 'WIM', 'ACP', 'Talented', 'Mapster',
}

KEYWORDS = [
    (('heal', 'cure', 'decurs', 'raid'), 'Healing'),
    (('plate', 'dps', 'damage', 'combat', 'cooldown', 'cd', 'spell', 'interrupt', 'proc'), 'Combat'),
    (('bag', 'inventory', 'mail', 'bank'), 'Bags & Mail'),
    (('chat', 'friend', 'emote', 'whisper', 'flood'), 'Social'),
    (('auction', 'market', 'trade', 'recipe', 'craft', 'prof'), 'Economy'),
    (('map', 'quest', 'minimap', 'coord'), 'Quests & Maps'),
    (('loot', 'gear', 'item', 'equip', 'tooltip', 'stat'), 'Loot & Gear'),
    (('arena', 'pvp', 'bg'), 'PvP'),
    (('bar', 'ui', 'frame', 'skin', 'key', 'bind'), 'Interface'),
]


def classify(name):
    if name in CURATED:
        return CURATED[name]
    low = name.lower()
    for keys, cat in KEYWORDS:
        if any(k in low for k in keys):
            return (cat, '')
    return ('Utility', '')


def main():
    req = urllib.request.Request(API, headers={'User-Agent': 'AzerothControl'})
    with urllib.request.urlopen(req, timeout=60) as r:
        entries = json.load(r)
    if isinstance(entries, dict):
        print('GitHub API said:', entries.get('message'))
        sys.exit(1)

    items, skipped = [], []
    for e in entries:
        n = e['name']
        if n.lower().endswith('.zip'):
            base = n[:-4]
            cat, desc = classify(base)
            items.append({
                'name': base,
                'file': n,
                'url': e['download_url'],
                'size': e.get('size', 0),
                'category': cat,
                'desc': desc,
                'recommended': base in RECOMMENDED,
            })
        elif n.lower().endswith('.rar'):
            skipped.append(n)

    items.sort(key=lambda x: (not x['recommended'], x['category'], x['name'].lower()))
    cats = {}
    for i in items:
        cats[i['category']] = cats.get(i['category'], 0) + 1

    payload = {'source': REPO, 'count': len(items), 'categories': cats,
               'skipped_rar': skipped, 'addons': items}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=None, separators=(',', ':'))

    print('catalog written:', OUT)
    print('addons:', len(items), ' recommended:', sum(1 for i in items if i['recommended']))
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        print('  %-16s %d' % (c, n))
    if skipped:
        print('skipped (.rar, no stdlib support):', ', '.join(skipped))


if __name__ == '__main__':
    main()
