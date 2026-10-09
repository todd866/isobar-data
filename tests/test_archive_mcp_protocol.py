"""JSON-RPC errors plus an actual python -m server round-trip over pipes."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from isobar_data.archive_mcp.protocol import MCPServer


class FakeStore:
    def list_runs(self):
        return {'families': {'ecmwf_ifs_global': []}}

    def sample(self, **kwargs):
        return {'value': 1.5, 'units': 'hPa', 'valid_time_utc': kwargs['valid_time_utc'], 'run_id': kwargs.get('run')}


def request(server, method, params=None, request_id=1):
    return server.handle({'jsonrpc':'2.0','id':request_id,'method':method,'params':params if params is not None else {}})


def server():
    instance=MCPServer(FakeStore())
    request(instance,'initialize',{'protocolVersion':'2025-06-18'})
    return instance


def test_initialize_negotiation_and_notifications():
    instance=MCPServer(FakeStore())
    assert request(instance,'tools/list')['error']['code']==-32602
    for version in ('2024-11-05','2025-03-26','2025-06-18'):
        assert request(instance,'initialize',{'protocolVersion':version})['result']['protocolVersion']==version
    assert request(instance,'initialize',{'protocolVersion':'future-version'})['result']['protocolVersion']=='2025-06-18'
    assert instance.handle({'jsonrpc':'2.0','method':'notifications/initialized'}) is None
    assert instance.handle({'jsonrpc':'2.0','method':'unknown_notification'}) is None


def test_tools_list_has_seven_read_only_tools():
    tools=request(server(),'tools/list')['result']['tools']
    assert {t['name'] for t in tools}=={'list_runs','sample','profile','section','run_change','forecast_vs_observed','facts'}
    assert all(t['annotations']['readOnlyHint'] for t in tools)
    assert all(not t['annotations']['openWorldHint'] for t in tools)


def test_tools_call_json_and_structured_content():
    params={'name':'sample','arguments':{'var':'mslp','lat':1,'lon':2,'valid_time_utc':'2026-01-01T00:00:00Z'}}
    result=request(server(),'tools/call',params)['result']
    assert json.loads(result['content'][0]['text'])==result['structuredContent']
    assert result['structuredContent']['units']=='hPa'
    old=MCPServer(FakeStore()); request(old,'initialize',{'protocolVersion':'2024-11-05'})
    assert 'structuredContent' not in request(old,'tools/call',params)['result']


@pytest.mark.parametrize('arguments', [[],None,False,{'extra':1}])
def test_non_object_and_unknown_arguments_rejected(arguments):
    reply=request(server(),'tools/call',{'name':'list_runs','arguments':arguments})
    assert reply['error']['code']==-32602


def test_errors_and_malformed_input_keep_protocol_alive():
    instance=server()
    assert request(instance,'tools/call',{'name':'missing','arguments':{}})['error']['code']==-32602
    assert request(instance,'unknown')['error']['code']==-32601
    assert instance.handle([])['error']['code']==-32600
    assert request(instance,'ping',[],request_id=2)['error']['code']==-32602
    output=io.StringIO()
    instance.run_stdio(io.StringIO('not-json\n{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),output)
    replies=[json.loads(line) for line in output.getvalue().splitlines()]
    assert [r.get('error',{}).get('code') for r in replies]==[-32700,-32700,None]
    assert replies[-1]['result']=={}


@pytest.mark.parametrize('update',[{'lat':True},{'lat':float('nan')},{'lon':'2'},{'png':True}])
def test_argument_types_and_unknown_fields(update):
    args={'var':'mslp','lat':1,'lon':2,'valid_time_utc':'2026-01-01T00:00:00Z'}; args.update(update)
    assert request(server(),'tools/call',{'name':'sample','arguments':args})['error']['code']==-32602


def test_stdio_real_module_round_trip():
    fixture=Path(__file__).parent/'fixtures/archive_mcp'
    commands=[
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'sample','arguments':{'var':'mslp','lat':-31.625,'lon':115.625,'valid_time_utc':'2026-10-08T00:00:00Z'}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'list_runs','arguments':{}}},
    ]
    proc=subprocess.run([sys.executable,'-m','isobar_data.archive_mcp','--store',str(fixture)],
        input='\n'.join(json.dumps(x) for x in commands)+'\n',text=True,capture_output=True,timeout=10)
    assert proc.returncode==0,proc.stderr
    assert proc.stderr==''
    replies=[json.loads(line) for line in proc.stdout.splitlines()]
    assert [r['id'] for r in replies]==[1,2,3,4]
    assert len(replies[1]['result']['tools'])==7
    value=replies[2]['result']['structuredContent']
    assert value['value'] is not None and value['units']=='hPa' and value['run_id']=='20261008T00Z'
    assert len(replies[3]['result']['structuredContent']['families']['ecmwf_ifs025'])==1


def test_invalid_rpc_version_and_nonfinite_id_cannot_break_stdio():
    output=io.StringIO()
    server().run_stdio(io.StringIO('{"jsonrpc":"wrong","id":1e999,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'),output)
    replies=[json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]['id'] is None and replies[0]['error']['code']==-32600
    assert replies[1]['result']=={}
