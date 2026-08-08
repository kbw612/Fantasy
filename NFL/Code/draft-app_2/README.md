# NFL Fantasy Draft Board

A Flask-backed draft board app supporting multiple concurrent drafts, CSV cheatsheet upload, and incremental pick imports from Sleeper.

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Start the server
python server.py

# 3. Open in your browser
open http://localhost:5001
```

## Usage

### Creating a draft
1. Click **+ New** in the draft selector bar at the top
2. Type a name (e.g. `Kevin_2025_League1`) and click **Create**
3. Your draft is now active — the status bar shows its name in green

### Switching between drafts
Use the **Draft** dropdown to switch. Each draft has its own picks and cheatsheet — they're completely independent.

### Loading a cheatsheet
1. Select your draft in the dropdown
2. Go to the **⬆ Cheatsheet** tab
3. Click **Browse for CSV** and select your rankings file

Expected CSV columns: `Rank, Player, Team, Position, Bye Week, ADP`
(This matches the PPR-cheatsheet.csv format)

### Importing picks
1. Go to the **⬆ Import Picks** tab
2. Select **My Fantasy Team** from the dropdown
3. Paste Sleeper draft results and click **Merge Picks**

Pick format:
```
Pick 1.1 - Pick: Bijan Robinson (RB - ATL)
Pick 1.2 - Pick: Christian McCaffrey (RB - SF)
```

**Smart merge**: paste picks round by round — existing pick labels are overwritten if you re-paste, new ones are appended. No need to re-paste already imported picks.

The running log at the bottom shows ALL picks accumulated for the active draft.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/drafts` | List all draft IDs |
| POST | `/drafts/create` | Create a new draft `{"id": "name"}` |
| GET | `/draft/<id>/picks` | Load all picks for a draft |
| POST | `/draft/<id>/picks` | Smart-merge new picks |
| DELETE | `/draft/<id>/picks` | Clear all picks |
| GET | `/draft/<id>/cheatsheet` | Load player pool |
| POST | `/draft/<id>/cheatsheet` | Upload CSV cheatsheet |

## Data storage

```
draft-app/
  server.py
  requirements.txt
  templates/
    draft-board-design1.html
  drafts/
    Kevin_2025_League1/
      picks.json        ← accumulated picks
      cheatsheet.json   ← player rankings
    Kevin_2025_League2/
      picks.json
      cheatsheet.json
```

Each draft is a folder under `drafts/`. Picks are stored as a JSON object keyed by pick label (`"1.03"`, `"2.12"`, etc.) so smart-merge just does `existing.update(new_picks)`.
