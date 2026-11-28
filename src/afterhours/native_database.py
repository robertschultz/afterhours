"""Read the complete VirtualDJ XML document before its native trailer bytes."""
import io


ROOT_END = b'</VirtualDJ_Database>'
XML_WHITESPACE = b' \t\r\n'


def complete_xml_prefix(source, limit):
    """Return a verified closed prefix or rewind the original stream."""
    size = source.seek(0, 2)
    if 0 < size <= limit:
        source.seek(max(0, size - 256))
        tail = source.read(256)
        closing = tail.rfind(ROOT_END)
        if closing >= 0:
            end = closing + len(ROOT_END)
            before_nul, nul, after_nul = tail[end:].partition(b'\x00')
            suffix = after_nul.strip(b'\x00' + XML_WHITESPACE)
            if (nul and not before_nul.strip(XML_WHITESPACE)
                    and len(suffix) < len(ROOT_END) and ROOT_END.endswith(suffix)):
                source.seek(0)
                snapshot = source.read(size + 1)
                if len(snapshot) == size and snapshot.endswith(tail):
                    return io.BytesIO(snapshot[:size-len(tail)+end])
    source.seek(0)
    return source
