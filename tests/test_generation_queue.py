from pathlib import Path

from songwriter.ai.generation import SongGeneration
from songwriter.web.generation_queue import GenerationQueue


class FakeCowriter:
    def __init__(self):
        self.songs = {}
        self.failure = None
        self.status = 'finalized'
        self.calls = 0

    def load_generation(self, song_id):
        return self.songs.get(song_id)

    def stream_song_interactive(self, song):
        self.calls += 1
        if self.failure:
            raise RuntimeError(self.failure)
        song.revision = (self.songs[song.song_id].revision if song.song_id in self.songs else 0) + 1
        song.status = self.status
        song.incomplete_sections = [{'name': 'Verse', 'reason': 'short'}] if self.status == 'incomplete' else []
        self.songs[song.song_id] = song
        yield {'type': 'complete', 'song': song.model_dump(mode='json')}


def test_quota_pause_resume_and_incomplete_retry(tmp_path: Path):
    writer = FakeCowriter()
    queue = GenerationQueue(tmp_path / 'queue.sqlite3', writer)
    job = queue.create([SongGeneration(title='First'), SongGeneration(title='Second')])
    writer.failure = 'HTTP 429: quota exhausted, reset in 10 minutes'
    assert queue._step()
    paused = queue.get(job['id'])
    assert paused['status'] == 'paused'
    assert '429' in paused['error']
    assert paused['items'][0]['status'] == 'queued'
    writer.failure = None
    writer.status = 'incomplete'
    queue.resume(job['id'], model='alternative-model')
    queue._step()
    first = queue.get(job['id'])['items'][0]
    assert first['status'] == 'incomplete'
    assert first['revision'] == 1
    assert writer.songs[first['song_id']].model == 'alternative-model'
    writer.status = 'finalized'
    queue.retry(job['id'], first['id'])
    queue._step()
    queue._step()
    queue._step()
    result = queue.get(job['id'])
    assert result['status'] == 'completed'
    assert result['items'][0]['song_id'] == first['song_id']
    assert result['items'][0]['revision'] == 2
    assert [item['status'] for item in result['items']] == ['completed', 'completed']
    assert len(writer.songs) == 2


def test_restart_reconciles_song_saved_before_queue_update(tmp_path: Path):
    writer = FakeCowriter()
    path = tmp_path / 'queue.sqlite3'
    queue = GenerationQueue(path, writer)
    job = queue.create([SongGeneration(title='Interrupted')])
    item = job['items'][0]
    with queue._connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
        db.execute("UPDATE items SET status='running',attempt_revision=0 WHERE id=?", (item['id'],))
    saved = SongGeneration(title='Interrupted', song_id=item['song_id'], revision=1, status='finalized')
    writer.songs[item['song_id']] = saved
    recovered = GenerationQueue(path, writer)
    assert recovered._step()
    assert recovered.get(job['id'])['items'][0]['status'] == 'completed'
    assert writer.calls == 0
