// Kindle Dashboard —— 读取 macOS Music.app / Apple Music 当前播放信息(JXA)
// 用法: osascript -l JavaScript read_music.js
// 输出:扁平 JSON,封面由 sync_music.sh 另行导出并拼入 artwork_data。
const music = Application("Music");

function safe(fn, fallback) {
  try {
    const v = fn();
    return (v === undefined || v === null) ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

function str(v) {
  return (v === undefined || v === null) ? "" : String(v);
}

function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

const sampledAt = Date.now() / 1000;

if (!safe(() => music.running(), false)) {
  JSON.stringify({
    has_track: false,
    state: "stopped",
    sampled_at: sampledAt
  });
} else {
  const state = str(safe(() => music.playerState(), "stopped")).toLowerCase();
  if (state === "stopped") {
    JSON.stringify({
      has_track: false,
      state: "stopped",
      sampled_at: sampledAt
    });
  } else {
    const t = music.currentTrack();
    const persistentId = str(safe(() => t.persistentID(), ""));
    const databaseId = num(safe(() => t.databaseID(), 0), 0);
    const name = str(safe(() => t.name(), ""));
    const artist = str(safe(() => t.artist(), ""));
    const album = str(safe(() => t.album(), ""));
    const duration = num(safe(() => t.duration(), 0), 0);
    const fallbackId = [name, artist, album, duration].join("|");
    JSON.stringify({
      has_track: true,
      state: state,
      sampled_at: sampledAt,
      position: num(safe(() => music.playerPosition(), 0), 0),
      duration: duration,
      shuffle: !!safe(() => music.shuffleEnabled(), false),
      repeat: str(safe(() => music.songRepeat(), "off")).toLowerCase(),
      track_id: persistentId || fallbackId,
      persistent_id: persistentId,
      database_id: databaseId,
      name: name,
      artist: artist,
      album: album,
      album_artist: str(safe(() => t.albumArtist(), "")),
      composer: str(safe(() => t.composer(), "")),
      genre: str(safe(() => t.genre(), "")),
      year: str(safe(() => t.year(), "")),
      track_number: num(safe(() => t.trackNumber(), 0), 0),
      track_count: num(safe(() => t.trackCount(), 0), 0),
      loved: !!safe(() => t.loved(), false),
      play_count: num(safe(() => t.playedCount(), 0), 0)
    });
  }
}
