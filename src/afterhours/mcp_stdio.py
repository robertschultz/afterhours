"""Optional MCP stdio adapter. Imports neither server.py nor provider code."""
import json
import logging
import os
import sys

from afterhours.mcp_auth import ControlError, client_config
from afterhours.mcp_client import ERRORS, request

INSTRUCTIONS = (
    'Control the existing local DJ through its conversation pipeline. Read state first; '
    'send one natural-language direction with its context and a stable requestId. '
    'Reuse an ID only with the identical payload, and poll its receipt after a timeout or disconnect. '
    'Pending and uncertain are not confirmation. Never retry uncertain work with a new ID. '
    'Current/cued recordings, manual ownership and configured client allowances remain authoritative. '
    'This connection cannot pair, widen budgets, start services, control microphones/cameras, or create video.'
)

SCHEMAS = {
    'afterhours_state':{'type':'object','properties':{
        'offset':{'type':'integer','minimum':0,'maximum':10000,'default':0},
        'limit':{'type':'integer','minimum':1,'maximum':24,'default':12}},'additionalProperties':False},
    'afterhours_direct':{'type':'object','properties':{
        'requestId':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,80}$'},
        'text':{'type':'string','minLength':1,'maxLength':1600},
        'context':{'type':'string','minLength':1,'maxLength':4096},
        'surface':{'type':['string','null'],'enum':[None,'catalog','settings','set']}},
        'required':['requestId','text','context'],'additionalProperties':False},
    'afterhours_receipt':{'type':'object','properties':{
        'requestId':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,80}$'}},
        'required':['requestId'],'additionalProperties':False},
}


def create_server(config):
    # The optional dependency is deliberately imported only by this entrypoint.
    import anyio
    from jsonschema import Draft202012Validator
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    descriptions={
        'afterhours_state':'Read bounded current/cued/queue and allowance state plus a 60-second context guard. No chat, paths or provider bodies.',
        'afterhours_direct':'Delegate a text direction through the same DJ engine as chat and voice. Uses this paired client’s music authority and keeps per-direction execution checks.',
        'afterhours_receipt':'Read the durable outcome for this client’s original request ID. Never replays work.',
    }
    async def list_tools(ctx,params):
        return ListToolsResult(tools=[Tool(name=name,description=descriptions[name],input_schema=schema,
            annotations=ToolAnnotations(read_only_hint=name!='afterhours_direct',destructive_hint=name=='afterhours_direct',
                                        idempotent_hint=True,open_world_hint=name=='afterhours_direct'))
            for name,schema in SCHEMAS.items()])

    async def call_tool(ctx,params):
        try:
            if params.name not in SCHEMAS or not Draft202012Validator(SCHEMAS[params.name]).is_valid(params.arguments or {}):
                raise ControlError('invalid-request',ERRORS['invalid-request'])
            # Network errors never trigger an automatic retry.
            result=await anyio.to_thread.run_sync(request,config['url'],config['credential'],
                                                 params.name.removeprefix('afterhours_'),params.arguments or {})
            return CallToolResult(content=[TextContent(type='text',text=json.dumps(result,separators=(',',':')))],
                                  structured_content=result)
        except ControlError as exc:
            result={'error':{'code':exc.code,'message':ERRORS.get(exc.code,ERRORS['unavailable'])}}
        except Exception:
            result={'error':{'code':'unavailable','message':ERRORS['unavailable']}}
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(result))],is_error=True)
    return Server('Afterhours DJ',version='1.0.0',instructions=INSTRUCTIONS,
                  on_list_tools=list_tools,on_call_tool=call_tool)


class BoundedInput:
    """Bound bytes before the SDK parses a newline-delimited frame."""
    def __init__(self,stream):self.stream=stream;self.closed=False
    def __aiter__(self):return self
    async def __anext__(self):
        import anyio
        if self.closed:raise StopAsyncIteration
        raw=await anyio.to_thread.run_sync(self.stream.readline,65537)
        if not raw:raise StopAsyncIteration
        try:
            if len(raw)>65536 or not raw.endswith(b'\n'):raise ValueError()
            text=raw.decode('utf-8')
            value=json.loads(text,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value,dict):raise ValueError()
            return text
        except (ValueError,UnicodeError,RecursionError):
            self.closed=True
            sys.stderr.write('Invalid or oversized MCP frame; connection closed.\n')
            raise StopAsyncIteration from None


class ProtocolOutput:
    """Protocol errors contain fixed copy, never SDK validation input excerpts."""
    def __init__(self,stream):self.stream=stream
    async def write(self,text):
        value=json.loads(text)
        if 'error' in value:
            value['error']={'code':value['error'].get('code',-32603),'message':'Invalid MCP request or unavailable operation.'}
        self.stream.write(json.dumps(value,separators=(',',':'))+'\n')
    async def flush(self):self.stream.flush()


class ProtocolLog(logging.Formatter):
    def format(self,record):
        return 'MCP protocol event; inspect the local DJ conversation for execution details.'


async def serve(config):
    from mcp.server.stdio import stdio_server
    server=create_server(config)
    # Explicit wire streams retain official SDK parsing/negotiation. Divert
    # incidental prints from stdout; neither input frames nor exceptions log.
    wire=os.fdopen(os.dup(sys.stdout.fileno()),'w',encoding='utf-8',buffering=1)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    try:
        async with stdio_server(stdin=BoundedInput(sys.stdin.buffer),stdout=ProtocolOutput(wire)) as (read,write):
            await server.run(read,write,server.create_initialization_options())
    finally:wire.close()


def main():
    try:
        if len(sys.argv)!=3 or sys.argv[1]!='--config':
            raise ValueError()
        config=client_config(sys.argv[2])
        handler=logging.StreamHandler(sys.stderr);handler.setFormatter(ProtocolLog())
        logging.basicConfig(level=logging.WARNING,handlers=[handler],force=True)
        import anyio
        anyio.run(serve,config)
        return 0
    except (KeyboardInterrupt,BrokenPipeError):return 0
    except BaseException:
        sys.stderr.write('Afterhours MCP could not start or its connection closed. Check the private configuration and optional SDK installation.\n')
        return 1


if __name__=='__main__':raise SystemExit(main())
