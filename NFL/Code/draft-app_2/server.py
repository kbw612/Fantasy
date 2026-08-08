"""
NFL Fantasy Draft Board — Flask Server
======================================
Run:  python server.py
App:  http://localhost:5001

Endpoints:
  GET  /                              — serves the draft board HTML
  GET  /drafts                        — list all draft IDs
  POST /drafts/create                 — create a new draft {"id": "Kevin_2025"}
  GET  /draft/<id>/picks              — load all accumulated picks
  POST /draft/<id>/picks              — smart-merge new picks (same label = overwrite)
  DELETE /draft/<id>/picks            — clear all picks for this draft
  GET  /draft/<id>/cheatsheet         — load player pool for this draft
  POST /draft/<id>/cheatsheet         — upload player rankings CSV for this draft
  GET  /teams                         — load teams + bye weeks
  POST /teams                         — upload teams CSV (Team, Bye Week)
  GET  /aliases                       — list all name aliases
  POST /aliases                       — add or update an alias {"sleeper": "D.J. Moore", "ranking": "DJ Moore"}
  DELETE /aliases                     — remove an alias {"sleeper": "D.J. Moore"}

Data layout on disk:
  teams.csv           Team, Bye Week — shared across all drafts
  drafts/
    <draft_id>/
      picks.json        { "1.01": {"pick":"1.01","name":"Ja'Marr Chase","pos":"WR","nflTeam":"CIN", ...}, ... }
      player-rankings.json   [ {"name":"Ja'Marr Chase","pos":"WR","team":"CIN","adp":3.1,"byeWeek":7,"rank":3,"projectedPoints":312.5,"positionRank":"WR3","fantasyPositions":"WR","age":"25","receiverDepth":"1","notes":""}, ... ]
      player-rankings.csv    raw uploaded CSV
"""

import os
import json
import csv
import io
import re
from flask import Flask, jsonify, request, send_from_directory, abort

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DRAFTS_DIR   = os.path.join(BASE_DIR, 'drafts')
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
TEAMS_CSV    = os.path.join(BASE_DIR, 'teams.csv')
ALIASES_CSV  = os.path.join(BASE_DIR, 'aliases.csv')

os.makedirs(DRAFTS_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# ── Name normalization ─────────────────────────────────────────────────────────

SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}

def normalize_name(name):
    """
    Normalize a player name for fuzzy matching.
    'D.J. Moore Jr.' -> 'dj moore'
    'AJ Brown' -> 'aj brown'
    'Jeffery Wilson Jr' -> 'jeffery wilson'
    """
    # Lowercase
    n = name.lower().strip()
    # Remove punctuation except spaces
    n = re.sub(r"[^a-z0-9 ]", '', n)
    # Split and drop known suffixes
    parts = [p for p in n.split() if p not in SUFFIXES]
    return ' '.join(parts)

def load_aliases():
    """
    Load aliases.csv -> dict mapping normalized sleeper name to rankings name.
    CSV columns: SleeperName, RankingsName
    """
    if not os.path.exists(ALIASES_CSV):
        return {}
    aliases = {}
    with open(ALIASES_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sleeper = row.get('SleeperName', '').strip()
            ranking = row.get('RankingsName', '').strip()
            if sleeper and ranking:
                aliases[normalize_name(sleeper)] = ranking
    return aliases

# ── Helpers ────────────────────────────────────────────────────────────────────

def draft_dir(draft_id):
    safe_id = re.sub(r'[^A-Za-z0-9_\-]', '_', draft_id)
    return os.path.join(DRAFTS_DIR, safe_id)

def picks_path(draft_id):
    return os.path.join(draft_dir(draft_id), 'picks.json')

def rankings_path(draft_id):
    return os.path.join(draft_dir(draft_id), 'player-rankings.json')

def rankings_csv_path(draft_id):
    return os.path.join(draft_dir(draft_id), 'player-rankings.csv')

def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def load_bye_weeks():
    """Load teams.csv and return a dict of {TEAM_ABBR: {byeWeek, offTier}}."""
    if not os.path.exists(TEAMS_CSV):
        return {}
    team_map = {}
    with open(TEAMS_CSV, 'r', encoding='utf-8-sig') as f:
        # Auto-detect delimiter
        sample = f.read(200); f.seek(0)
        delimiter = '\t' if '\t' in sample else ','
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            team = (row.get('Team') or row.get('team') or '').strip().upper()
            bye  = row.get('Bye Week') or row.get('bye_week') or row.get('Bye') or 0
            tier = row.get('Offensive Tier') or row.get('offensive_tier') or row.get('Tier') or 2
            if team:
                team_map[team] = {
                    'byeWeek': int(float(bye or 0)),
                    'offTier': int(float(tier or 2)),
                }
    return team_map

def parse_teams_csv(text):
    """Parse a teams CSV/TSV (Team, Bye Week, Offensive Tier) and return list of dicts."""
    teams = []
    dialect = '\t' if '\t' in text[:200] else ','
    reader = csv.DictReader(io.StringIO(text), delimiter=dialect)
    for row in reader:
        team = (row.get('Team') or row.get('team') or '').strip().upper()
        bye  = row.get('Bye Week') or row.get('bye_week') or row.get('Bye') or 0
        tier = row.get('Offensive Tier') or row.get('offensive_tier') or row.get('Tier') or 2
        if team:
            teams.append({
                'team':    team,
                'byeWeek': int(float(bye or 0)),
                'offTier': int(float(tier or 2)),
            })
    return teams

def parse_cheatsheet_csv(text, bye_map):
    """
    Parse player rankings CSV (offensive and/or defensive players).
    Columns: Rank, Player, Team, Position, ADP, Projected Points,
             Position Rank, Fantasy Positions, Age, Receiver Depth, Notes
    Bye Week is joined from teams.csv.
    """
    players = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            name             = row.get('Player', '').strip()
            team             = row.get('Team', '').strip().upper()
            pos              = row.get('Position', '').strip()
            adp              = float(row.get('ADP', 0) or 0)
            rank             = int(row.get('Rank', 0) or 0)
            projectedPoints  = float(row.get('Projected Points', 0) or
                                     row.get('Proj Points', 0) or
                                     row.get('Projection', 0) or 0)
            byeWeek          = bye_map.get(team, {}).get('byeWeek', 0) if isinstance(bye_map.get(team), dict) else bye_map.get(team, 0)
            positionRank     = (row.get('Position Rank') or row.get('position_rank') or '').strip()
            fantasyPositions = (row.get('Fantasy Positions') or row.get('fantasy_positions') or '').strip()
            age              = (row.get('Age') or row.get('age') or '').strip()
            receiverDepth    = (row.get('Receiver Depth') or row.get('receiver_depth') or '').strip()
            notes            = (row.get('Notes') or row.get('notes') or '').strip()
            if name and pos:
                players.append({
                    'name':             name,
                    'pos':              pos,
                    'team':             team,
                    'adp':              adp,
                    'byeWeek':          byeWeek,
                    'rank':             rank,
                    'projectedPoints':  projectedPoints,
                    'positionRank':     positionRank,
                    'fantasyPositions': fantasyPositions,
                    'age':              age,
                    'receiverDepth':    receiverDepth,
                    'notes':            notes,
                })
        except (ValueError, KeyError):
            continue
    return players

# ── CORS ───────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,DELETE,OPTIONS'
    return response

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(TEMPLATE_DIR, 'draft-board-design1.html')

# ── Teams endpoints ────────────────────────────────────────────────────────────

@app.route('/teams', methods=['GET'])
def get_teams():
    """Return teams, bye weeks and offensive tiers from teams.csv."""
    team_map = load_bye_weeks()
    if not team_map:
        return jsonify([])
    return jsonify([
        {'team': t, 'byeWeek': v['byeWeek'], 'offTier': v['offTier']}
        for t, v in sorted(team_map.items())
    ])

@app.route('/teams', methods=['POST'])
def upload_teams():
    """
    Upload teams CSV (Team, Bye Week). Saves as teams.csv in the app root.
    Accepts tab or comma delimited. Applies to all drafts.
    """
    if 'file' in request.files:
        text = request.files['file'].read().decode('utf-8-sig')
    elif request.content_type and 'text' in request.content_type:
        text = request.get_data(as_text=True)
    else:
        return jsonify({'error': 'Send CSV/TSV as file upload or text body'}), 400

    teams = parse_teams_csv(text)
    if not teams:
        return jsonify({'error': 'No valid teams found'}), 400

    with open(TEAMS_CSV, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['Team', 'Bye Week', 'Offensive Tier'])
        writer.writeheader()
        for t in teams:
            writer.writerow({'Team': t['team'], 'Bye Week': t['byeWeek'], 'Offensive Tier': t['offTier']})

    return jsonify({'loaded': len(teams), 'teams': teams})

# ── Aliases endpoints ──────────────────────────────────────────────────────────

@app.route('/aliases', methods=['GET'])
def get_aliases():
    """Return all aliases as a list of {sleeper, ranking} objects."""
    if not os.path.exists(ALIASES_CSV):
        return jsonify([])
    aliases = []
    with open(ALIASES_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sleeper = row.get('SleeperName', '').strip()
            ranking = row.get('RankingsName', '').strip()
            if sleeper and ranking:
                aliases.append({'sleeper': sleeper, 'ranking': ranking})
    return jsonify(aliases)

@app.route('/aliases', methods=['POST'])
def save_alias():
    """Add or update an alias. Body: {"sleeper": "D.J. Moore", "ranking": "DJ Moore"}"""
    body = request.get_json(silent=True) or {}
    sleeper = body.get('sleeper', '').strip()
    ranking = body.get('ranking', '').strip()
    if not sleeper or not ranking:
        return jsonify({'error': 'sleeper and ranking are required'}), 400

    # Load existing aliases
    aliases = []
    if os.path.exists(ALIASES_CSV):
        with open(ALIASES_CSV, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                s = row.get('SleeperName', '').strip()
                r = row.get('RankingsName', '').strip()
                if s and s != sleeper:  # skip existing entry for this sleeper name
                    aliases.append({'SleeperName': s, 'RankingsName': r})

    aliases.append({'SleeperName': sleeper, 'RankingsName': ranking})

    with open(ALIASES_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['SleeperName', 'RankingsName'])
        writer.writeheader()
        writer.writerows(aliases)

    return jsonify({'saved': True, 'sleeper': sleeper, 'ranking': ranking})

@app.route('/aliases', methods=['DELETE'])
def delete_alias():
    """Remove an alias. Body: {"sleeper": "D.J. Moore"}"""
    body = request.get_json(silent=True) or {}
    sleeper = body.get('sleeper', '').strip()
    if not sleeper:
        return jsonify({'error': 'sleeper name required'}), 400

    aliases = []
    if os.path.exists(ALIASES_CSV):
        with open(ALIASES_CSV, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                s = row.get('SleeperName', '').strip()
                r = row.get('RankingsName', '').strip()
                if s and s != sleeper:
                    aliases.append({'SleeperName': s, 'RankingsName': r})

    with open(ALIASES_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['SleeperName', 'RankingsName'])
        writer.writeheader()
        writer.writerows(aliases)

    return jsonify({'deleted': True, 'sleeper': sleeper})

# ── Drafts ─────────────────────────────────────────────────────────────────────

@app.route('/drafts', methods=['GET'])
def list_drafts():
    if not os.path.exists(DRAFTS_DIR):
        return jsonify([])
    drafts = []
    for name in sorted(os.listdir(DRAFTS_DIR)):
        d = os.path.join(DRAFTS_DIR, name)
        if os.path.isdir(d):
            has_picks = os.path.exists(os.path.join(d, 'picks.json'))
            has_sheet = os.path.exists(os.path.join(d, 'player-rankings.json'))
            pick_count = 0
            if has_picks:
                picks = load_json(os.path.join(d, 'picks.json'), {})
                pick_count = len(picks)
            drafts.append({
                'id':            name,
                'hasPicks':      has_picks,
                'hasCheatsheet': has_sheet,
                'pickCount':     pick_count
            })
    return jsonify(drafts)

@app.route('/drafts/create', methods=['POST'])
def create_draft():
    body = request.get_json(silent=True) or {}
    draft_id = body.get('id', '').strip()
    if not draft_id:
        return jsonify({'error': 'Draft ID is required'}), 400
    d = draft_dir(draft_id)
    if os.path.exists(d):
        return jsonify({'error': f'Draft "{draft_id}" already exists'}), 409
    os.makedirs(d, exist_ok=True)
    return jsonify({'id': os.path.basename(d), 'created': True}), 201

# ── Picks ──────────────────────────────────────────────────────────────────────

@app.route('/draft/<draft_id>/picks', methods=['GET'])
def get_picks(draft_id):
    if not os.path.exists(draft_dir(draft_id)):
        abort(404, description=f'Draft "{draft_id}" not found')
    return jsonify(load_json(picks_path(draft_id), {}))

@app.route('/draft/<draft_id>/picks', methods=['POST'])
def merge_picks(draft_id):
    if not os.path.exists(draft_dir(draft_id)):
        abort(404, description=f'Draft "{draft_id}" not found')
    body = request.get_json(silent=True) or {}
    new_picks = body.get('picks', {})
    if not new_picks:
        return jsonify({'error': 'No picks provided'}), 400
    existing = load_json(picks_path(draft_id), {})
    existing.update(new_picks)
    save_json(picks_path(draft_id), existing)
    return jsonify({'merged': len(new_picks), 'total': len(existing), 'picks': existing})

@app.route('/draft/<draft_id>/picks', methods=['DELETE'])
def clear_picks(draft_id):
    if not os.path.exists(draft_dir(draft_id)):
        abort(404, description=f'Draft "{draft_id}" not found')
    body = request.get_json(silent=True) or {}
    picks = load_json(picks_path(draft_id), {})
    if 'label' in body:
        # Delete a single pick by label (e.g. "2.10")
        picks.pop(body['label'], None)
        save_json(picks_path(draft_id), picks)
        return jsonify({'deleted': body['label'], 'picks': picks})
    else:
        # Clear all picks
        save_json(picks_path(draft_id), {})
        return jsonify({'cleared': True})

# ── Cheatsheet ─────────────────────────────────────────────────────────────────

@app.route('/draft/<draft_id>/cheatsheet', methods=['GET'])
def get_cheatsheet(draft_id):
    if not os.path.exists(draft_dir(draft_id)):
        abort(404, description=f'Draft "{draft_id}" not found')
    path = rankings_path(draft_id)
    if not os.path.exists(path):
        return jsonify([])
    return jsonify(load_json(path, []))

@app.route('/draft/<draft_id>/cheatsheet', methods=['POST'])
def upload_cheatsheet(draft_id):
    """
    Upload player rankings CSV. Bye Week is joined from teams.csv automatically.
    Columns: Rank, Player, Team, Position, ADP, Projected Points
    """
    if not os.path.exists(draft_dir(draft_id)):
        abort(404, description=f'Draft "{draft_id}" not found')

    filename = 'player-rankings.csv'
    if 'file' in request.files:
        f = request.files['file']
        filename = f.filename or filename
        csv_text = f.read().decode('utf-8-sig')
    elif request.content_type and 'text' in request.content_type:
        csv_text = request.get_data(as_text=True)
    else:
        return jsonify({'error': 'Send CSV as file upload or text body'}), 400

    bye_map = load_bye_weeks()
    if not bye_map:
        return jsonify({'error': 'Upload teams.csv first so bye weeks can be looked up'}), 400

    players = parse_cheatsheet_csv(csv_text, bye_map)
    if not players:
        return jsonify({'error': 'No valid players found in CSV'}), 400

    save_json(rankings_path(draft_id), players)
    with open(rankings_csv_path(draft_id), 'w', encoding='utf-8') as fout:
        fout.write(csv_text)

    return jsonify({'loaded': len(players), 'filename': filename, 'players': players})

# ── Draft Settings endpoints ────────────────────────────────────────────────────

def settings_path(draft_id):
    return os.path.join(draft_dir(draft_id), 'settings.json')

@app.route('/draft/<draft_id>/settings', methods=['GET'])
def get_settings(draft_id):
    """Load saved settings for a draft."""
    app.logger.info(f'GET settings v7 draft_id={repr(draft_id)}')
    return jsonify(load_json(settings_path(draft_id), {}))

@app.route('/draft/<draft_id>/settings', methods=['POST'])
def post_settings(draft_id):
    """Save settings for a draft."""
    app.logger.info(f'POST settings draft_id={repr(draft_id)}')
    body = request.get_json(silent=True) or {}
    os.makedirs(draft_dir(draft_id), exist_ok=True)
    existing = load_json(settings_path(draft_id), {})
    existing.update(body)
    save_json(settings_path(draft_id), existing)
    return jsonify({'saved': True, 'settings': existing})

# ── Error handlers ─────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': str(e)}), 404

@app.errorhandler(409)
def conflict(e):
    return jsonify({'error': str(e)}), 409

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)
    print("=" * 55)
    print("  NFL Fantasy Draft Board Server")
    print("  Open http://localhost:5001 in your browser")
    print("=" * 55)
    app.run(debug=False, port=5001, threaded=True)
