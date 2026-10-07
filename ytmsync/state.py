"""Cache locale. Non è la fonte di verità: se manca o è corrotta si riparte senza perdere brani."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 1


def empty_state() -> dict:
    return {
        "version": VERSION,
        "spotify_playlist_id": None,
        "ytm_playlist_id": None,
        # Coppie sincronizzate: base per capire se un brano manca perché rimosso su Spotify
        "pairs": [],  # {"sp": uri, "sp_key": str, "yt": videoId, "yt_key": str}
        "ai_verdicts": {},  # "sorgente||candidato" -> bool
        "not_found": {},  # id sorgente -> {"attempts": int, "last": iso}
        "pending_removals": {},  # videoId -> numero di esecuzioni consecutive in cui risulta da rimuovere
        "consecutive_failures": 0,
        "ai_unavailable_runs": 0,
        "alerts": {},
        "last_success": None,
    }


def load(path: Path) -> tuple[dict, bool]:
    """Restituisce (stato, cache_valida). Con cache non valida niente rimozioni in questa esecuzione."""
    if not path.exists():
        return empty_state(), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise ValueError(f"versione {data.get('version')}")
        state = empty_state() | data
        return state, True
    except Exception as e:  # noqa: BLE001
        log.warning("Cache illeggibile (%s): riparto da zero, nessuna rimozione in questa esecuzione", e)
        try:
            path.rename(path.with_suffix(".corrupt.json"))
        except OSError:
            pass
        return empty_state(), False


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
