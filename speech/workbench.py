import hashlib
import io
import json
import re
import sqlite3
import time
import uuid
import wave
from contextlib import contextmanager


class Conflict(ValueError):
    pass


def validate_segments(segments,duration):
    if not isinstance(segments,list) or len(segments)>10000 or type(duration) is not int or duration<1:
        raise ValueError("Invalid segments or duration")
    end=0
    for item in segments:
        if not isinstance(item,dict) or set(item)!={"start","end","text"}:
            raise ValueError("Each segment requires start/end milliseconds and text")
        if type(item["start"]) is not int or type(item["end"]) is not int or not end<=item["start"]<item["end"]<=duration:
            raise ValueError("Segments must be ordered, nonoverlapping and within audio duration")
        value=item["text"]
        if not isinstance(value,str) or not value.strip() or len(value)>2000 or "\r" in value or "\x00" in value or any(not line.strip() for line in value.split("\n")):
            raise ValueError("Subtitle text must be nonempty lines up to 2000 characters")
        end=item["end"]
    if len(json.dumps(segments,ensure_ascii=False).encode())>4*1024*1024:
        raise ValueError("Transcript exceeds 4 MiB")
    return json.loads(json.dumps(segments,ensure_ascii=False))


def stamp(ms):
    hours,rest=divmod(ms,3600000);minutes,rest=divmod(rest,60000);seconds,milli=divmod(rest,1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"


def to_srt(segments,duration):
    segments=validate_segments(segments,duration)
    return "\n\n".join(str(i)+"\n"+stamp(s["start"])+" --> "+stamp(s["end"])+"\n"+s["text"] for i,s in enumerate(segments,1))+("\n" if segments else "")


def from_srt(content,duration):
    if not isinstance(content,str) or len(content.encode())>8*1024*1024:
        raise ValueError("SRT exceeds 8 MiB")
    content=content.removeprefix("\ufeff").replace("\r\n","\n").strip("\n")
    if not content:
        return validate_segments([],duration)
    segments=[]
    timing=re.compile(r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})")
    for i,block in enumerate(content.split("\n\n"),1):
        lines=block.split("\n")
        if len(lines)<3 or lines[0]!=str(i):
            raise ValueError("Use sequential numbered SRT cues")
        match=timing.fullmatch(lines[1])
        if not match:
            raise ValueError("Invalid SRT timestamp")
        values=list(map(int,match.groups()))
        convert=lambda p:((p[0]*60+p[1])*60+p[2])*1000+p[3]
        segments.append({"start":convert(values[:4]),"end":convert(values[4:]),"text":"\n".join(lines[2:])})
    return validate_segments(segments,duration)


def label(value,maxlen=100):
    if not isinstance(value,str) or not value.strip() or len(value)>maxlen or "\x00" in value:
        raise ValueError("Invalid label")
    return value.strip()


class Workbench:
    def __init__(self,path,clock=time.time):
        self.path,self.clock=str(path),clock
        with self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS audio(id TEXT PRIMARY KEY,name TEXT NOT NULL,data BLOB NOT NULL,duration INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,audio TEXT NOT NULL REFERENCES audio(id),engine TEXT NOT NULL,
                event TEXT UNIQUE NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL,due REAL NOT NULL,
                token TEXT,lease REAL,segments TEXT,original TEXT,revision INTEGER NOT NULL,error TEXT);
                CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES jobs(id),revision INTEGER NOT NULL,
                actor TEXT NOT NULL,reason TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL);""")

    @contextmanager
    def db(self,write=False):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None);db.row_factory=sqlite3.Row;db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:
            db.close()

    def ingest(self,name,data):
        name=label(name,255)
        if not isinstance(data,bytes) or not 1<=len(data)<=10*1024*1024:
            raise ValueError("Use PCM WAV bytes up to 10 MiB")
        try:
            with wave.open(io.BytesIO(data),"rb") as source:
                if source.getcomptype()!="NONE" or source.getsampwidth() not in (1,2,3,4) or not 1<=source.getnchannels()<=2 or not 1000<=source.getframerate()<=192000:
                    raise ValueError("Supported audio: mono/stereo PCM WAV, 1..4 byte samples,1..192 kHz")
                frames=source.getnframes();rate=source.getframerate();expected=frames*source.getnchannels()*source.getsampwidth()
                if expected>10*1024*1024 or len(source.readframes(frames))!=expected:
                    raise ValueError("Truncated or oversized PCM audio")
                duration=frames*1000//rate
                if duration<1:
                    raise ValueError("Audio must contain at least 1 ms")
        except (wave.Error,EOFError) as exc:
            raise ValueError("Invalid PCM WAV") from exc
        ident=hashlib.sha256(data).hexdigest()
        with self.db(True) as db:
            db.execute("INSERT OR IGNORE INTO audio VALUES(?,?,?,?)",(ident,name,data,duration))
        return ident

    def enqueue(self,audio,engine,event):
        engine,event=label(engine),label(event)
        with self.db(True) as db:
            if not db.execute("SELECT id FROM audio WHERE id=?",(audio,)).fetchone():
                raise KeyError(audio)
            row=db.execute("SELECT * FROM jobs WHERE event=?",(event,)).fetchone()
            if row:
                if row["audio"]!=audio or row["engine"]!=engine:
                    raise Conflict("Event already used for another audio/engine")
                return row["id"]
            ident=uuid.uuid4().hex
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,0,NULL)",(ident,audio,engine,event,self.clock()))
            return ident

    def claim(self,*,lease=60,max_attempts=3):
        if type(lease) is not int or not 1<=lease<=3600 or type(max_attempts) is not int or not 1<=max_attempts<=10:
            raise ValueError("Invalid worker settings")
        with self.db(True) as db:
            db.execute("UPDATE jobs SET state='pending',token=NULL,lease=NULL WHERE state='running' AND lease<=?",(self.clock(),))
            db.execute("UPDATE jobs SET state='dead' WHERE state='pending' AND attempts>=?",(max_attempts,))
            row=db.execute("SELECT * FROM jobs WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 1",(self.clock(),)).fetchone()
            if not row:
                return None
            token=uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='running',token=?,lease=?,attempts=attempts+1 WHERE id=?",(token,self.clock()+lease,row["id"]))
            audio=db.execute("SELECT * FROM audio WHERE id=?",(row["audio"],)).fetchone()
            if hashlib.sha256(audio["data"]).hexdigest()!=audio["id"]:
                raise ValueError("Audio integrity mismatch")
            return {"id":row["id"],"token":token,"engine":row["engine"],"data":audio["data"],"duration":audio["duration"]}

    def settle(self,job,token,*,segments=None,error=None,max_attempts=3):
        if type(max_attempts) is not int or not 1<=max_attempts<=10 or (error is not None and not isinstance(error,str)):
            raise ValueError("Invalid settlement")
        with self.db(True) as db:
            row=db.execute("SELECT j.*,a.duration FROM jobs j JOIN audio a ON a.id=j.audio WHERE j.id=?",(job,)).fetchone()
            if row is None or row["state"]!="running" or row["token"]!=token or row["lease"]<=self.clock():
                raise Conflict("Worker no longer owns job lease")
            if error is None:
                encoded=json.dumps(validate_segments(segments,row["duration"]),ensure_ascii=False)
                db.execute("UPDATE jobs SET state='ready',segments=?,original=?,token=NULL,lease=NULL,error=NULL WHERE id=?",(encoded,encoded,job))
            else:
                state="dead" if row["attempts"]>=max_attempts else "pending"
                db.execute("UPDATE jobs SET state=?,due=?,error=?,token=NULL,lease=NULL WHERE id=?",(state,self.clock()+2**row["attempts"],error[:100],job))

    def process_one(self,adapters,*,lease=60,max_attempts=3):
        row=self.claim(lease=lease,max_attempts=max_attempts)
        if row is None:
            return None
        try:
            segments=adapters[row["engine"]](row["data"],row["duration"])
            validate_segments(segments,row["duration"])
        except Exception as exc:
            self.settle(row["id"],row["token"],error=type(exc).__name__,max_attempts=max_attempts)
        else:
            self.settle(row["id"],row["token"],segments=segments,max_attempts=max_attempts)
        return self.get(row["id"])

    def get(self,job):
        with self.db() as db:
            row=db.execute("SELECT j.id,j.audio,j.engine,j.event,j.state,j.attempts,j.due,j.revision,j.error,j.segments,j.original,a.duration FROM jobs j JOIN audio a ON a.id=j.audio WHERE j.id=?",(job,)).fetchone()
            if row is None:
                raise KeyError(job)
            result=dict(row)
            for field in ("segments","original"):
                result[field]=json.loads(result[field]) if result[field] is not None else None
            return result

    def edit(self,job,segments,*,expected_revision,actor,reason):
        actor,reason=label(actor),label(reason,500)
        if type(expected_revision) is not int or expected_revision<0:
            raise ValueError("Invalid revision")
        with self.db(True) as db:
            row=db.execute("SELECT j.*,a.duration FROM jobs j JOIN audio a ON a.id=j.audio WHERE j.id=?",(job,)).fetchone()
            if row is None:
                raise KeyError(job)
            if row["state"]!="ready" or row["revision"]!=expected_revision:
                raise Conflict("Only current ready transcripts may be edited")
            encoded=json.dumps(validate_segments(segments,row["duration"]),ensure_ascii=False)
            db.execute("UPDATE jobs SET segments=?,revision=revision+1 WHERE id=?",(encoded,job))
            db.execute("INSERT INTO reviews(job,revision,actor,reason,before_json,after_json) VALUES(?,?,?,?,?,?)",(job,expected_revision+1,actor,reason,row["segments"],encoded))

    def history(self,job):
        self.get(job)
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT revision,actor,reason,before_json,after_json FROM reviews WHERE job=? ORDER BY revision",(job,))]

    def export(self,job,*,format="srt"):
        row=self.get(job)
        if row["state"]!="ready":
            raise Conflict("Transcript is not ready")
        if format=="srt":
            return to_srt(row["segments"],row["duration"])
        if format=="vtt":
            cues=[]
            for segment in validate_segments(row["segments"],row["duration"]):
                cues.append(stamp(segment["start"]).replace(",",".")+" --> "+stamp(segment["end"]).replace(",",".")+"\n"+segment["text"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
            return "WEBVTT\n\n"+"\n\n".join(cues)+("\n" if cues else "")
        raise ValueError("Supported exports: srt/vtt")
