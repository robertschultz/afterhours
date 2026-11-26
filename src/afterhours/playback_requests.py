"""Explicit operator playback intent; model prose never grants transport."""
import re


NAMED_PLAY = re.compile(r'^(?:play|start playing|start (?:a |the )?(?:new )?(?:dj )?(?:session|set|mix) (?:based on|inspired by|with|of|around|featuring))\s+(\S.*)$')


def named_query(text):
    """Use the named portion of an explicit play request for a catalog lookup."""
    if intent(text) != 'select-and-start':return None
    match=re.fullmatch(r'(?:play|start playing)\s+(\S.*)',normalized_intent(text))
    if not match:return None
    query=re.sub(r'^(?:some|something (?:by|from)|a track (?:by|from))\s+', '', match[1])
    query=re.sub(r'(?:,? now|,? right now)$', '', query).strip()
    return query if 2<=len(query)<=120 and ';' not in query else None


def named_recording(query):
    if not isinstance(query,str):return None,None
    title, separator, artist = query.rpartition(' by ')
    return (title, artist) if separator and title and artist else (None, None)


def ready_opening(snapshot):
    """Go may start only the exact applied opening that has never run.

    This selects the existing start path; its native readback, cue validation
    and cleanup checks still own permission to execute audio.
    """
    order=[t.get('id') for t in snapshot.get('order',[])]
    plan=snapshot.get('executionPlan') or {}
    return (snapshot.get('phase')=='ready' and snapshot.get('active') is False
            and snapshot.get('index')==-1 and snapshot.get('startedAt') is None
            and snapshot.get('endedAt') is None and not snapshot.get('resumeBlocked')
            and not snapshot.get('queuedUpdate')
            and (snapshot.get('copilot') or {}).get('mode') in ('leading','mix')
            and len(order)>=2 and all(order) and len(set(order))==len(order)
            and plan.get('applied') is True and bool(plan.get('id'))
            and plan.get('orderIds')==order)


def queue_intent(value):
    """Match a complete existing-queue instruction, never a named-music prefix.

    Continuation is distinct because a held/manual set must use the guarded
    handback path rather than loading its opening track again.
    """
    value=re.sub(r'^(?:(?:just|go ahead and) )+', '', value)
    queue=r'(?:the |our )?(?:(?:current|retained|ready|loaded|existing|prepared|selected|saved) )?queue'
    if re.fullmatch(r'(?:start(?: playing)?|play) '+queue+r'(?: now)?',value):return 'start'
    if (value in ('keep going','keep the music going','continue playing')
            or re.fullmatch(r'(?:continue|resume)(?: playing)? '+queue+r'(?: now)?',value)
            or re.fullmatch(r'keep '+queue+r' going',value)
            or re.fullmatch(r'keep playing '+queue,value)):
        return 'continue'
    return None


def normalized_intent(text):
    if not isinstance(text, str):return None
    value=' '.join(text.casefold().replace('’',"'").split()).strip(' .!?')
    value=re.sub(r'^(?:hey )?dot[, ]+', '', value)
    value=re.sub(r'^(?:ok(?:ay)?[, ]+|yes[, ]+)', '', value)
    value=re.sub(r'^(?:can|could|would|will) (?:you|we) (?:please )?', '', value)
    value=re.sub(r'^please |[, ]+please$', '', value).strip()
    value=re.sub(r"^let'?s (?:go ahead and )?", '', value)
    # A conversational lead-in does not turn a plain transport command into
    # paid discovery. Keep the complete remainder: named music, timing and
    # negation still pass through the checks below.
    value=re.sub(r'^go (?:ahead and )?', '', value)
    return value


def fresh_voice_request(text, snapshot):
    command=normalized_intent(text)
    if not command or queue_intent(command):return False
    meaning=intent(text)
    if meaning not in ('start','select-and-start','fresh-start'):return False
    if meaning=='select-and-start' and not named_query(text):return False
    return (not snapshot.get('active') and
            not any(deck.get('playing') for deck in (snapshot.get('telemetry') or {}).get('decks', [])))


def intent(text):
    value=normalized_intent(text)
    if not value:return None
    if value in {'status','what is playing',"what's playing",'what are you playing',
                 'are you playing','is music playing','are you ready','what are you doing',
                 "what's happening",'what is happening','why are you still analyzing',
                 "why aren't you playing",'why are you not playing','how is it going',
                 'are you starting','when will the music start'}:return 'status'
    stop_value=re.sub(r',? (?:right now|now)$', '', value)
    if stop_value in {'stop', 'stop playing', 'stop the music', 'stop music', 'stop the set',
                 'pause the music', 'pause music', 'cancel playback', 'cancel start',
                 'pause', 'pause it', 'pause playback', 'pause the set', 'pause the track', 'pause this',
                 'stop it', 'stop the track', 'stop playback', 'stop this', 'hold the music'}:return 'stop'
    if (re.search(r"\b(?:not|never|later|tomorrow|tonight|after|when|until|unless|before|once|if|don't|dont)\b",value)
            or re.search(r'\bin (?:\d+|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty|sixty) (?:seconds?|minutes?|hours?|days?)\b',value)):
        return None
    existing=queue_intent(value)
    if existing:return existing
    if re.fullmatch(r'(?:start|play) (?:a )?(?:new|fresh) (?:queue|set)(?: now)?',value):
        return 'fresh-start'
    if value in {'play', 'play music', 'play the music', 'play now', 'play it', 'play the set',
                 'play the queue', 'start', 'start playing', 'start playing music', 'start music',
                 'start the music', 'start the set', 'start set', 'start the queue', 'start djing',
                 'start playing now', 'start it', 'start it now', 'go ahead and start',
                 "let's start playing", 'lets start playing',"let's play music",'lets play music',
                 'play some music','start some music','start playing some music','play something',
                 'put some music on','put the music on','get the music going',
                 'start a session','start the session','start a dj session','start a set',
                 'start djing now','just play','just play music','just start playing'}:return 'start'
    # A request naming music must first pass normal discovery/selection. Never
    # start the old queue just because the sentence begins with "play".
    if NAMED_PLAY.fullmatch(value):return 'select-and-start'
    return None
