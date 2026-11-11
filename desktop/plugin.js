import { Codicon, ROUTES_AREA, SIDEBAR_NAV_AREA, pluginSettingsHref, writeClipboardText } from '@hermes/plugin-sdk'
import React, { useEffect, useRef, useState } from 'react'

const h = React.createElement
const REGIONS = ['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-central-1', 'ap-south-1', 'ap-southeast-1', 'ap-northeast-1', 'ca-central-1']
const SIZES = ['t3.micro', 't3.small', 't3.medium', 't3.large', 'm7i.large']
const RUNTIMES = ['python3.12', 'python3.13', 'python3.14']
const SERVICES = {
  ec2: { title: 'EC2', label: 'Instances', icon: 'server', create: 'Launch instance' },
  lambda: { title: 'Lambda', label: 'Functions', icon: 'symbol-function', create: 'Create function' },
  s3: { title: 'S3', label: 'Buckets', icon: 'database', create: 'Create bucket' }
}
const TABS = [['overview', 'Overview'], ['ec2', 'EC2'], ['lambda', 'Lambda'], ['s3', 'S3'], ['activity', 'Activity']]
const STARTER = 'def lambda_handler(event, context):\n    return {"message": "Hello from Hermes", "event": event}\n'
const styles = `
.aws-console{--bg:var(--dt-background,#101318);--panel:var(--dt-card,#171b21);--line:var(--dt-border,#2e3540);--ink:var(--dt-foreground,#f0f3f8);--sub:var(--dt-muted-foreground,#a1abb8);--blue:var(--dt-primary,#82aaff);--deep:#11161f;--green:#5cd6ad;--amber:#ffd18b;--red:#ff9b9b;box-sizing:border-box;background:var(--bg);color:var(--ink);font:14px/1.5 Inter,system-ui,sans-serif;height:100%;overflow:auto;padding:26px clamp(15px,3vw,38px) 42px;color-scheme:dark}
.aws-console *{box-sizing:border-box}.aws-console h1,.aws-console h2,.aws-console h3,.aws-console p{margin:0}.aws-console h1{font-size:27px;letter-spacing:-.65px;line-height:1.18}.aws-console h2{font-size:16px;font-weight:620}.aws-console h3{font-size:13px}.aws-console .sub{color:var(--sub)}.aws-console .mono{font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}.aws-console .frame{max-width:1440px;margin:0 auto}
.aws-console .row{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.aws-console .between{justify-content:space-between}.aws-console .header{gap:15px;margin-bottom:15px}.aws-console .brand{display:flex;gap:12px;align-items:center}.aws-console .brand-icon{display:grid;place-items:center;width:42px;height:42px;border:1px solid #405878;background:#192639;color:#9bbaff;border-radius:8px}.aws-console .brand p{margin-top:5px;font-size:12px}
.aws-console button,.aws-console select,.aws-console input,.aws-console textarea,.aws-console a.button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:7px 11px}.aws-console button,.aws-console a.button{cursor:pointer;white-space:nowrap;text-decoration:none}.aws-console button:hover,.aws-console a.button:hover{border-color:var(--blue)}.aws-console button:disabled{opacity:.46;cursor:default}.aws-console .primary{background:#9bbaff;color:#121c2d;border-color:#9bbaff;font-weight:650}.aws-console .danger{color:var(--red)}.aws-console .ghost{background:transparent}.aws-console button:focus-visible,.aws-console select:focus-visible,.aws-console input:focus-visible,.aws-console textarea:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.aws-console .scope{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:11px 13px;margin:15px 0 6px;min-height:58px}.aws-console .scope-label{font-size:12px}.aws-console .mode{background:var(--deep);border:1px solid var(--line);padding:2px;border-radius:7px;gap:2px}.aws-console .mode button{border-color:transparent;background:transparent;padding:4px 11px;color:var(--sub)}.aws-console .mode button[aria-pressed=true]{background:#223047;color:#dbe6ff}
.aws-console nav{display:flex;gap:3px;overflow-x:auto;border-bottom:1px solid var(--line);margin-bottom:18px}.aws-console nav button{background:transparent;border:0;border-radius:0;border-bottom:2px solid transparent;color:var(--sub);padding:10px 14px}.aws-console nav button[aria-current=page]{color:var(--ink);border-color:var(--blue)}
.aws-console .cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px;margin-bottom:14px}.aws-console .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:0;overflow:hidden}.aws-console .pad{padding:15px}.aws-console .tile-icon{display:inline-grid;place-items:center;width:28px;height:28px;background:#222e42;color:#b2c9fc;border-radius:6px}.aws-console .tile h2{font-size:14px}.aws-console .stat{font-size:30px;font-weight:630;line-height:1.15;margin:11px 0 4px}.aws-console .tile button{margin-top:12px}.aws-console .card-head{padding:13px 15px;border-bottom:1px solid var(--line);min-height:47px}.aws-console .table-wrap{overflow:auto}.aws-console table{border-collapse:collapse;width:100%;white-space:nowrap;text-align:left}.aws-console th{font-size:12px;color:var(--sub);font-weight:520;background:color-mix(in srgb,var(--panel) 80%,var(--bg));padding:10px 15px}.aws-console td{padding:10px 15px;border-top:1px solid var(--line);font-size:13px}.aws-console td .sub{display:block;font-size:11px}.aws-console .resource{border:0!important;background:none;padding:0!important;color:var(--ink);text-align:left;font-weight:620}.aws-console .resource:hover{text-decoration:underline}.aws-console .active-row{background:#21304755}.aws-console .empty{padding:22px 15px;color:var(--sub)}.aws-console .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(285px,.63fr);gap:13px;align-items:start}.aws-console .stack{display:grid;gap:13px}.aws-console .actions{margin-top:13px}
.aws-console .status{font-size:12px;display:inline-flex;gap:7px;align-items:center}.aws-console .status:before{content:'';display:block;width:7px;height:7px;border-radius:50%;background:currentColor}.aws-console .status.good{color:var(--green)}.aws-console .status.warn{color:var(--amber)}.aws-console .status.bad{color:var(--red)}
.aws-console table{color:var(--ink)}
.aws-console .notice{border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:11px 13px;margin:0 0 13px;font-size:13px}.aws-console .notice.error{border-color:#6e4146;color:var(--red)}.aws-console .notice.warn{border-color:#66543b;color:var(--amber)}.aws-console .notice.success{border-color:#366253;color:var(--green)}
.aws-console dl{margin:8px 0 0}.aws-console .fact{display:grid;grid-template-columns:minmax(85px,.5fr) minmax(0,1fr);gap:10px;padding:7px 0;border-bottom:1px solid var(--line);font-size:12px}.aws-console .fact dt{color:var(--sub)}.aws-console .fact dd{margin:0;overflow-wrap:anywhere;text-align:right}.aws-console pre{margin:9px 0 0;padding:10px;border:1px solid var(--line);border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto;background:var(--deep);font-size:12px}
.aws-console .overlay{position:fixed;z-index:1000;inset:0;display:grid;place-items:center;background:#080d17bb;padding:18px}.aws-console .dialog{width:min(630px,100%);max-height:calc(100vh - 42px);overflow:auto;padding:18px;box-shadow:0 20px 100px #000a}.aws-console .dialog .between{margin-bottom:12px}.aws-console .fields{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}.aws-console .field{display:grid;gap:4px;min-width:0}.aws-console .field.wide{grid-column:1/-1}.aws-console .field label{font-size:12px;color:var(--sub)}.aws-console .field input,.aws-console .field select,.aws-console .field textarea{width:100%;background:var(--bg)}.aws-console .field textarea{resize:vertical;min-height:100px;font-family:ui-monospace,monospace;font-size:12px}.aws-console .field input[type=checkbox]{width:auto;margin-right:5px}.aws-console .dialog .foot{display:flex;justify-content:flex-end;gap:8px;margin-top:15px;flex-wrap:wrap}
@media(max-width:980px){.aws-console .layout{grid-template-columns:1fr}.aws-console .card-head input{width:min(170px,100%)}}@media(max-width:680px){.aws-console .cards{grid-template-columns:1fr}.aws-console .fields{grid-template-columns:1fr}.aws-console .scope{align-items:flex-start}.aws-console nav button{padding-inline:9px}}
`

function status(value) {
  const normalized = String(value || 'available').toLowerCase()
  const tone = ['active', 'running', 'available', 'completed', 'connected', 'created', 'verified in aws', 'verified in demo'].includes(normalized) ? 'good' :
    ['failed', 'unverified', 'terminated', 'deleted'].includes(normalized) ? 'bad' :
    ['pending', 'submitted', 'stopped', 'partial', 'stopping', 'starting'].includes(normalized) ? 'warn' : 'sub'
  return h('span', { className: 'status ' + tone }, value || 'available')
}

function icon(name) {
  return Codicon ? h(Codicon, { name }) : null
}

function button(text, onClick, className = '', disabled = false) {
  return h('button', { type: 'button', onClick, className, disabled }, text)
}

function options(items) {
  return items.map(item => {
    const [value, label] = Array.isArray(item) ? item : [item, item]
    return h('option', { key: value, value }, label)
  })
}

function errorText(error) {
  return String(error?.message || error || 'Request failed.').slice(0, 400)
}

function date(value) {
  return value ? new Date(value).toLocaleString() : '—'
}

function size(value) {
  if (value < 1024) return value + ' B'
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KiB'
  return (value / 1024 / 1024).toFixed(1) + ' MiB'
}

function facts(values) {
  return h('dl', null, ...values.map(([label, value]) => h('div', { className: 'fact', key: label },
    h('dt', null, label), h('dd', null, value === null || value === undefined || value === '' ? '—' : value))))
}

function table(columns, rows, empty) {
  return rows.length ? h('div', { className: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, columns.map(column => h('th', { key: column }, column)))),
    h('tbody', null, rows))) : h('p', { className: 'empty' }, empty)
}

function cellRow(key, cells, selected = false) {
  return h('tr', { key, className: selected ? 'active-row' : '' },
    cells.map((cell, index) => h('td', { key: index }, cell)))
}

function settingsLink() {
  const href = typeof pluginSettingsHref === 'function' ? pluginSettingsHref('aws-manager') : '#/settings'
  return h('a', { className: 'button', href }, 'AWS credentials')
}

function Workspace({ api, scope, tab, setTab }) {
  const [connection, setConnection] = useState(null)
  const [connectionIssue, setConnectionIssue] = useState('')
  const [data, setData] = useState({})
  const [history, setHistory] = useState([])
  const [busy, setBusy] = useState(true)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  const [operation, setOperation] = useState(null)
  const [notice, setNotice] = useState(null)
  const request = useRef(0)
  const scopeQuery = new URLSearchParams(scope).toString()

  function get(path, extra = {}) {
    return api(path + '?' + new URLSearchParams({ ...scope, ...extra }))
  }

  async function refresh() {
    const serial = ++request.current
    setBusy(true)
    setConnectionIssue('')
    try {
      const identity = await get('/connection')
      if (serial !== request.current) return
      setConnection(identity)
      const entries = await Promise.all(Object.keys(SERVICES).map(async kind => {
        try {
          return [kind, await get('/inventory/' + kind)]
        } catch (error) {
          return [kind, { items: [], error: errorText(error) }]
        }
      }))
      const events = await get('/activity').catch(() => ({ items: [] }))
      if (serial === request.current) {
        setData(Object.fromEntries(entries))
        setHistory(events.items)
      }
    } catch (error) {
      if (serial === request.current) {
        setConnection(null)
        setConnectionIssue(errorText(error))
        setData({})
        setHistory([])
      }
    } finally {
      if (serial === request.current) setBusy(false)
    }
  }

  useEffect(() => {
    void refresh()
    return () => { request.current += 1 }
  }, [scopeQuery])

  async function next(kind) {
    const serial = request.current
    const page = await get('/inventory/' + kind, { cursor: data[kind].cursor }).catch(error => {
      if (serial === request.current) setNotice({ tone: 'error', message: errorText(error) })
    })
    if (page && serial === request.current) {
      setData(current => {
        const items = new Map(current[kind].items.map(row => [row.id, row]))
        page.items.forEach(row => items.set(row.id, row))
        return { ...current, [kind]: { ...page, items: [...items.values()] } }
      })
    }
  }

  const canWrite = Boolean(connection && !connection.root)

  function open(kind, title, values = {}) {
    setNotice(null)
    setOperation({ kind, title, values })
  }

  function select(kind, id) {
    setTab(kind)
    setSelected(id)
    setQuery('')
  }

  function switchTab(nextTab) {
    setTab(nextTab)
    setSelected(null)
    setQuery('')
  }

  function after(receipt) {
    const message = receipt.error || [receipt.title, receipt.result?.id, receipt.result?.state].filter(Boolean).join(' · ')
    setNotice({ tone: receipt.status === 'completed' ? 'success' : 'error', message, result: receipt.result })
    setOperation(null)
    if (receipt.status === 'completed' && /\.(create|connect)$/.test(receipt.action)) {
      select(receipt.action.split('.')[0], receipt.result?.id)
    }
    if (receipt.status === 'completed' && ['ec2.terminate', 'lambda.delete', 's3.delete'].includes(receipt.action)) {
      setSelected(null)
    }
    void refresh()
  }

  async function verify(id) {
    const receipt = await api('/activity/' + encodeURIComponent(id) + '/verify?' + scopeQuery, { method: 'POST' })
    setHistory(current => current.map(item => item.id === receipt.id ? receipt : item))
  }

  const selectedItem = data[tab]?.items?.find(item => item.id === selected)
  const services = Object.keys(SERVICES)
  const createFor = kind => open(kind + '.create', SERVICES[kind].create)

  return h(React.Fragment, null,
    h('div', { className: 'scope row between' },
      h('div', { className: 'row scope-label' },
        status(connection ? (scope.mode === 'demo' ? 'Demo workspace' : 'connected') : busy ? 'connecting' : 'disconnected'),
        h('span', { className: 'sub' }, scope.mode === 'demo' ? 'Sample resources' : connection?.account ? 'Account ' + connection.account : 'Connect an AWS identity')),
      h('div', { className: 'row' }, scope.mode === 'live' && settingsLink(), button(busy ? 'Refreshing…' : 'Refresh', refresh, '', busy))),
    h('nav', { 'aria-label': 'AWS services' }, ...TABS.map(([id, label]) =>
      h('button', { type: 'button', key: id, 'aria-current': tab === id ? 'page' : undefined, onClick: () => switchTab(id) }, label))),
    connectionIssue && h('div', { className: 'notice error', role: 'alert' }, connectionIssue),
    connection?.root && h('div', { className: 'notice warn' }, 'Connected as AWS account root. Use scoped IAM credentials for changes.'),
    notice && h('div', { className: 'notice ' + notice.tone, role: 'status' },
      h('div', { className: 'row between' }, notice.message, button('Dismiss', () => setNotice(null), 'ghost')),
      notice.result?.output !== undefined && h('pre', null, notice.result.output + (notice.result.truncated ? '\n[Response truncated]' : '')),
      notice.result?.request_id && h('div', { className: 'sub' }, 'AWS request ID ' + notice.result.request_id),
      notice.result?.log && h('details', null, h('summary', null, 'Invocation log'), h('pre', null, notice.result.log)),
      notice.result?.arn && h('div', { className: 'mono' }, notice.result.arn)),
    tab === 'overview' && h(React.Fragment, null,
      h('div', { className: 'cards' }, ...services.map(kind => {
        const item = SERVICES[kind]
        const collection = data[kind]
        return h('section', { className: 'card pad tile', key: kind },
          h('div', { className: 'row' }, h('span', { className: 'tile-icon' }, icon(item.icon)),
            h('h2', null, item.title + ' ' + item.label.toLowerCase())),
          h('div', { className: 'stat' }, collection && !collection.error ? collection.items.length + (collection.cursor ? '+' : '') : '—'),
          h('div', { className: 'sub' }, collection?.error ? 'Inventory unavailable' : scope.region),
          h('div', { className: 'row' },
            button(item.create, () => createFor(kind), 'primary', !canWrite),
            button('View ' + item.label.toLowerCase(), () => switchTab(kind), 'ghost')))
      })),
      h(Activity, { items: history.slice(0, 6), verify, go: () => switchTab('activity') })),
    tab === 'activity' && h(Activity, { items: history, verify }),
    SERVICES[tab] && h('div', { className: selectedItem ? 'layout' : '' },
      h('section', { className: 'card' },
        h('div', { className: 'card-head row between' },
          h('h2', null, SERVICES[tab].label),
          h('div', { className: 'row' },
            h('input', { 'aria-label': 'Filter resources', placeholder: 'Filter ' + SERVICES[tab].label.toLowerCase(),
              value: query, onChange: event => setQuery(event.target.value) }),
            tab === 's3' && button('Connect bucket', () => open('s3.connect', 'Connect existing bucket'), '', !canWrite),
            button(SERVICES[tab].create, () => createFor(tab), 'primary', !canWrite))),
        data[tab]?.error ? h('p', { className: 'empty', role: 'alert' }, data[tab].error) :
          h(React.Fragment, null, data[tab]?.warning && h('p', { className: 'empty' }, data[tab].warning),
            h(ResourceTable, { kind: tab, items: data[tab]?.items || [], query, selected, select: id => setSelected(id) })),
        data[tab]?.cursor && h('div', { className: 'pad' }, button('Load more', () => next(tab))),
        tab === 'ec2' && h('div', { className: 'pad row' },
          button('Create default VPC', () => open('ec2.default_vpc', 'Create default VPC'), '', !canWrite),
          button('Create security group', () => open('ec2.security_group', 'Create security group'), '', !canWrite),
          button('Import public key', () => open('ec2.key_import', 'Import public key'), '', !canWrite)),
        tab === 'lambda' && h('div', { className: 'pad' },
          button('Create execution role', () => open('lambda.role_create', 'Create Lambda execution role'), '', !canWrite))),
      selectedItem && h(ResourceDetail, { key: tab + ':' + selectedItem.id, kind: tab, item: selectedItem, get,
        open, canWrite, region: scope.region, close: () => setSelected(null), version: history[0]?.id + ':' + history[0]?.status })),
    operation && h(Operation, { key: operation.kind, operation, api, scope, get, after, close: () => setOperation(null) }))
}

function Activity({ items, verify, go }) {
  const [checking, setChecking] = useState(null)
  const [issue, setIssue] = useState(null)

  async function check(id) {
    setChecking(id)
    setIssue(null)
    try {
      await verify(id)
    } catch (error) {
      setIssue({ id, message: errorText(error) })
    } finally {
      setChecking(null)
    }
  }

  return h('section', { className: 'card' },
    h('div', { className: 'card-head row between' },
      h('h2', null, 'Activity'), go && button('View activity', go, 'ghost')),
    table(['Action', 'Target', 'Status', 'Submitted'], items.map(item => {
      const evidence = item.result?.verification
      let label = item.status
      if (evidence) label = item.mode === 'live' ? 'Verified in AWS' : 'Verified in Demo'
      else if (item.can_verify) label = 'Submitted'
      return cellRow(item.id, [
        h(React.Fragment, null, item.title, item.error && h('span', { className: 'sub' }, item.error),
          item.result?.request_id && h('span', { className: 'sub mono' }, 'AWS request ID ' + item.result.request_id)),
        h('span', { className: 'mono' }, item.target),
        h(React.Fragment, null, status(label),
          evidence && h('span', { className: 'sub', title: date(evidence.checked) }, evidence.detail),
          item.can_verify && h('div', { className: 'row' },
            button(checking === item.id ? 'Checking…' : 'Verify', () => check(item.id), 'ghost', checking !== null)),
          issue?.id === item.id && h('span', { className: 'sub', role: 'alert' }, issue.message)),
        date(item.submitted)
      ])
    }), 'Resource changes will appear here.'))
}

function ResourceTable({ kind, items, query, selected, select }) {
  const filter = query.trim().toLowerCase()
  const visible = items.filter(item => (item.id + ' ' + (item.name || '')).toLowerCase().includes(filter))
  const headings = kind === 'ec2' ? ['Instance', 'State', 'Type', 'Zone'] :
    kind === 'lambda' ? ['Function', 'State', 'Runtime', 'Memory'] : ['Bucket', 'Connection', 'Region', 'Created']
  return table(headings, visible.map(item => {
    const target = h('button', { className: 'resource', onClick: () => select(item.id) },
      item.name || item.id, item.name && item.name !== item.id && h('span', { className: 'sub mono' }, item.id))
    const columns = kind === 'ec2' ? [status(item.state), item.type, item.zone || '—'] :
      kind === 'lambda' ? [status(item.state), item.runtime, item.memory ? item.memory + ' MB' : '—'] :
        [item.connected ? status('connected') : '—', item.region, date(item.created)]
    return cellRow(item.id, [target, ...columns], item.id === selected)
  }), filter ? 'No matching resources.' : 'No resources found in this region.')
}

function ResourceDetail({ kind, item, get, open, canWrite, close, version, region }) {
  const [details, setDetails] = useState(null)
  const [logs, setLogs] = useState(null)
  const [issue, setIssue] = useState('')
  const [copyState, setCopyState] = useState('')
  const data = { ...item, ...details }
  const identifier = kind === 'ec2' ? { id: item.id } : { name: item.id }
  const action = (verb, label, extra = {}, css = '', disabled = false) =>
    button(label, () => open(kind + '.' + verb, label, { ...identifier, ...extra }), css, !canWrite || disabled)

  useEffect(() => {
    let active = true
    if (kind === 'lambda') get('/lambda/detail/' + encodeURIComponent(item.id)).then(value => {
      if (active) setDetails(value)
    }).catch(error => { if (active) setIssue(errorText(error)) })
    return () => { active = false }
  }, [item.id, kind, version])

  async function copy(text) {
    try {
      await writeClipboardText(text)
      setCopyState('Copied')
    } catch (error) {
      setIssue(errorText(error))
    }
  }

  if (kind === 's3') return h(Bucket, { item, get, open, canWrite, close, version })
  const panels = kind === 'ec2' ? [
    ['ID', item.id], ['Type', item.type], ['Availability zone', item.zone], ['Private IP', item.private_ip],
    ['Public IP', item.public_ip], ['VPC', item.vpc], ['Subnet', item.subnet], ['Security groups', item.groups?.join(', ')],
    ['AMI', item.ami], ['Key pair', item.key_pair], ['Instance profile', item.instance_profile]
  ] : [['Runtime', data.runtime], ['Memory', data.memory ? data.memory + ' MB' : '—'],
    ['Timeout', data.timeout ? data.timeout + ' s' : '—'],
    ['Last update', data.update_status], ['Role', data.role], ['ARN', data.arn],
    ['Function URL', data.url || data.url_status], ['URL authentication', data.url_auth]]

  return h('section', { className: 'card' },
    h('div', { className: 'card-head row between' }, h('h2', null, item.name || item.id), button('Close', close, 'ghost')),
    h('div', { className: 'pad' }, status(data.state), facts(panels),
      issue && h('p', { role: 'alert', className: 'sub' }, issue),
      kind === 'ec2' ? h(React.Fragment, null,
        h('div', { className: 'row actions' },
          action('start', 'Start', {}, '', item.state !== 'stopped'), action('stop', 'Stop', {}, '', item.state !== 'running'),
          action('reboot', 'Reboot', {}, '', item.state !== 'running'),
          action('resize', 'Change type', { type: item.type }, '', item.state !== 'stopped'),
          action('rename', 'Rename', { name: item.name || '' }),
          action('terminate', 'Terminate', {}, 'danger')),
        h('div', { className: 'row actions' },
          item.instance_profile && button('Copy SSM command', () => copy('aws ssm start-session --region ' + region + ' --target ' + item.id)),
          item.public_ip && item.key_pair && button('Copy SSH command', () => copy('ssh ec2-user@' + item.public_ip)),
          copyState && h('span', { className: 'sub', role: 'status' }, copyState)))
        : h(React.Fragment, null,
          h('div', { className: 'row actions' },
            action('invoke', 'Test invoke', {}, 'primary', data.state === 'Pending'),
            action('deploy', 'Deploy code'), action('configure', 'Edit settings', { memory: data.memory, timeout: data.timeout }),
            button('View logs', async () => {
              try {
                setIssue('')
                setLogs((await get('/lambda/logs/' + encodeURIComponent(item.id))).events)
              } catch (error) {
                setIssue(errorText(error))
              }
            }), action('delete', 'Delete', {}, 'danger')),
          details && !data.url_status && h('div', { className: 'row actions' },
            data.url ? h(React.Fragment, null,
              button(copyState || 'Copy URL', () => copy(data.url)), action('url_delete', 'Remove function URL', {}, 'danger'))
              : action('url_create', 'Create function URL')),
          logs && h('pre', null, logs.map(item => date(item.at) + '  ' + item.message).join('\n') || 'No recent events.'))))
}

function Bucket({ item, get, open, canWrite, close, version }) {
  const [page, setPage] = useState(null)
  const [prefix, setPrefix] = useState('')
  const [appliedPrefix, setAppliedPrefix] = useState('')
  const [issue, setIssue] = useState('')
  const [file, setFile] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const serial = useRef(0)
  const encoded = encodeURIComponent(item.id)
  const action = (verb, title, values = {}, css = '') =>
    button(title, () => open('s3.' + verb, title, { bucket: item.id, ...values }), css, !canWrite)

  useEffect(() => {
    const id = ++serial.current
    setIssue('')
    setPage(null)
    get('/s3/objects/' + encoded, { prefix: appliedPrefix }).then(data => {
      if (serial.current === id) setPage(data)
    }).catch(error => { if (serial.current === id) setIssue(errorText(error)) })
    return () => { serial.current += 1 }
  }, [encoded, version, appliedPrefix])

  useEffect(() => () => { if (file) URL.revokeObjectURL(file.url) }, [file])

  async function fetchFile(key) {
    const id = serial.current
    setDownloading(true)
    setIssue('')
    setFile(null)
    try {
      const { name, content, sha256, matches_upload } = await get('/s3/download/' + encoded, { key })
      if (serial.current !== id) return
      const bytes = Uint8Array.from(atob(content), char => char.charCodeAt(0))
      setFile({ name, sha256, matchesUpload: matches_upload,
        url: URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' })) })
    } catch (error) {
      if (serial.current === id) setIssue(errorText(error))
    } finally {
      setDownloading(false)
    }
  }

  return h('section', { className: 'card' },
    h('div', { className: 'card-head row between' }, h('h2', null, item.id), button('Close', close, 'ghost')),
    h('div', { className: 'pad stack' },
      h('div', { className: 'row' }, item.connected && status('connected'),
        action(item.connected ? 'disconnect' : 'connect', item.connected ? 'Disconnect' : 'Connect bucket'),
        action('upload', 'Upload file', {}, 'primary'), action('delete', 'Delete empty bucket', {}, 'danger')),
      h('form', { className: 'row', onSubmit: event => { event.preventDefault(); setAppliedPrefix(prefix) } },
        h('input', { 'aria-label': 'Object prefix', placeholder: 'Filter by prefix', value: prefix,
          onChange: event => setPrefix(event.target.value) }),
        h('button', { type: 'submit' }, 'Filter')),
      issue && h('p', { role: 'alert', className: 'notice error' }, issue),
      file && h('div', { className: 'row' }, h('a', { className: 'button primary', href: file.url, download: file.name }, 'Save ' + file.name),
        file.matchesUpload === true && h('span', { className: 'sub', title: file.sha256 }, 'SHA-256 matches upload'),
        file.matchesUpload === false && h('span', { className: 'sub', role: 'status' }, 'Current file differs from the last upload in Hermes'))),
    page && table(['Object', 'Size', 'Actions'], page.items.map(object => cellRow(object.key, [
      h('span', { className: 'mono' }, object.key), size(object.size),
      h('div', { className: 'row' }, button('Download', () => fetchFile(object.key), '', downloading),
        action('remove', 'Delete', { key: object.key, etag: object.etag }, 'danger'))
    ])), 'No objects match this prefix.'),
    page?.cursor && h('div', { className: 'pad' }, button('Load more objects', async () => {
      try {
        const id = serial.current
        const result = await get('/s3/objects/' + encoded, { prefix: appliedPrefix, cursor: page.cursor })
        if (id === serial.current) setPage({ ...result, items: [...page.items, ...result.items] })
      } catch (error) {
        setIssue(errorText(error))
      }
    })))
}

const ACTION_HELP = {
  'ec2.create': 'Launch one Amazon Linux 2023 instance with an encrypted gp3 root disk. The security group you select controls access.',
  'ec2.default_vpc': 'AWS creates a default VPC, public subnets, and an internet gateway for this region.',
  'ec2.security_group': 'Create a security group. Add a restricted SSH source if you need inbound SSH.',
  'ec2.key_import': 'Import only the public key. Keep the private key on your device.',
  'ec2.stop': 'The instance will shut down. Attached storage can continue to incur charges.',
  'ec2.reboot': 'Reboot the operating system on the selected instance.',
  'ec2.terminate': 'Terminate this instance permanently. AWS deletes volumes configured to be deleted on termination.',
  'lambda.create': 'Deploy a Python function using an existing Lambda execution role.',
  'lambda.deploy': 'Replace the function code with this Python handler.',
  'lambda.invoke': 'Run the selected function with the test event. The function can make changes using its own execution role.',
  'lambda.role_create': 'Create an IAM role that Lambda can assume, with CloudWatch Logs access scoped to the named function.',
  'lambda.delete': 'Permanently delete this function and its versions.',
  'lambda.url_create': 'Create an HTTPS endpoint that requires AWS IAM authorization.',
  'lambda.url_delete': 'Remove the HTTPS endpoint. The function can still be invoked through the Lambda API.',
  's3.create': 'Create a general purpose bucket in the selected region. AWS applies its private bucket defaults.',
  's3.connect': 'Verify access to an existing bucket in this account and save it in this workspace.',
  's3.disconnect': 'Remove the saved connection. The bucket and its objects stay in AWS.',
  's3.upload': 'Upload a file up to 4 MiB. If an object already exists at this key, the upload fails.',
  's3.remove': 'Delete the current object at this key. In a versioned bucket, AWS can retain earlier versions.',
  's3.delete': 'Permanently delete this bucket. AWS requires it to be empty, including versions and delete markers.'
}
const LABELS = {
  id: 'Instance', name: 'Name', type: 'Instance type', public_ip: 'Public IPv4', security_group: 'Security group',
  subnet: 'Subnet', disk_gib: 'Root disk (GiB)', key_pair: 'Key pair', instance_profile: 'Instance profile',
  ami: 'AMI', root_device: 'Root device', runtime: 'Runtime', role: 'Execution role', memory: 'Memory (MB)',
  timeout: 'Timeout (seconds)', bucket: 'Bucket', key: 'Object key', etag: 'Object version check', vpc: 'VPC',
  function: 'Function', revision: 'Revision', ssh_cidr: 'SSH source'
}

function initial(kind, supplied) {
  const common = {
    'ec2.create': { name: '', type: 't3.micro', subnet: '', security_group: '', disk_gib: 8,
      public_ip: false, key_pair: '', instance_profile: '' },
    'ec2.security_group': { name: '', vpc: '', ssh_cidr: '' },
    'ec2.key_import': { name: '', payload: '' },
    'lambda.create': { name: '', runtime: 'python3.13', role: '', memory: 128, timeout: 10, payload: STARTER },
    'lambda.deploy': { payload: STARTER },
    'lambda.invoke': { payload: '{\n  "source": "hermes"\n}' },
    'lambda.role_create': { name: '', function: '' },
    's3.upload': { key: '', payload: '' }
  }
  return { ...(common[kind] || {}), ...supplied }
}

function Operation({ operation, api, scope, get, after, close }) {
  const { kind, title } = operation
  const [values, setValues] = useState(() => initial(kind, operation.values))
  const [extras, setExtras] = useState(null)
  const [extrasError, setExtrasError] = useState('')
  const [plan, setPlan] = useState(null)
  const [confirmation, setConfirmation] = useState('')
  const [issue, setIssue] = useState('')
  const [fileName, setFileName] = useState('')
  const [busy, setBusy] = useState(false)
  const dialog = useRef(null)
  const active = useRef(true)

  useEffect(() => {
    active.current = true
    const frame = dialog.current
    if (frame && !frame.open) frame.showModal()
    const path = ['ec2.create', 'ec2.security_group'].includes(kind) ? '/network' :
      kind === 'lambda.create' ? '/lambda/roles' : null
    if (path) get(path).then(result => { if (active.current) setExtras(result) })
      .catch(error => { if (active.current) setExtrasError(errorText(error)) })
    return () => { active.current = false; if (frame?.open) frame.close() }
  }, [kind])

  function change(key, value) {
    setValues(previous => ({ ...previous, [key]: value, ...(key === 'subnet' ? { security_group: '' } : {}) }))
  }

  function field(key, label, config = {}) {
    const { type = 'text', list, wide, hint, ...attrs } = config
    const props = { id: 'aws-field-' + key, ...attrs }
    let input
    if (list) input = h('select', { ...props, value: values[key] || '',
      onChange: event => change(key, event.target.value) }, options(list))
    else if (type === 'textarea') input = h('textarea', { ...props, value: values[key] || '',
      onChange: event => change(key, event.target.value) })
    else input = h('input', { ...props, type, value: values[key] ?? '',
      onChange: event => change(key, type === 'number' ? (event.target.value === '' ? '' : Number(event.target.value)) : event.target.value) })
    return h('div', { className: 'field' + (wide ? ' wide' : ''), key },
      h('label', { htmlFor: props.id }, label), input, hint && h('small', { className: 'sub' }, hint))
  }

  async function fileSelected(event) {
    const file = event.target.files?.[0]
    setFileName('')
    change('payload', '')
    if (!file) return
    if (file.size > 4 * 1024 * 1024) {
      setIssue('Choose a file up to 4 MiB.')
      return
    }
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      const chunks = []
      for (let start = 0; start < bytes.length; start += 8192) {
        chunks.push(String.fromCharCode(...bytes.subarray(start, start + 8192)))
      }
      if (!active.current) return
      setIssue('')
      setValues(previous => ({ ...previous, key: previous.key || file.name, payload: btoa(chunks.join('')) }))
      setFileName(file.name)
    } catch (error) {
      setIssue(errorText(error))
    }
  }

  async function submit(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setIssue('')
    try {
      if (!plan) {
        const result = await api('/plans', { method: 'POST', body: { scope, action: kind, values } })
        if (active.current) setPlan(result)
      } else {
        const result = await api('/plans/' + plan.id + '/apply', {
          method: 'POST', body: { confirmation, payload: values.payload || '' }
        })
        if (active.current) after(result)
      }
    } catch (error) {
      if (active.current) setIssue(errorText(error))
    } finally {
      if (active.current) setBusy(false)
    }
  }

  const subnet = extras?.subnets?.find(item => item.id === values.subnet)
  const controls = []
  const required = { required: true }
  if (kind === 'ec2.create') controls.push(
    field('name', 'Instance name', { ...required, maxLength: 64, placeholder: 'api-worker' }),
    field('type', 'Instance type', { ...required, list: SIZES }),
    field('subnet', 'Subnet', { ...required, list: [['', 'Choose a subnet'], ...(extras?.subnets || []).map(s => [s.id, (s.name || s.id) + ' · ' + s.zone])] }),
    field('security_group', 'Security group', { ...required, list: [['', 'Choose a security group'], ...(extras?.groups || [])
      .filter(group => group.vpc === subnet?.vpc).map(group => [group.id, group.name + ' · ' + group.id])] }),
    field('disk_gib', 'Root disk (GiB)', { ...required, type: 'number', min: 8, max: 128 }),
    field('key_pair', 'Key pair (optional)', { list: [['', 'None'], ...(extras?.key_pairs || [])] }),
    field('instance_profile', 'IAM instance profile ARN (optional)', { wide: true, maxLength: 255 }),
    h('label', { className: 'field wide', key: 'public_ip' }, h('span', null,
      h('input', { type: 'checkbox', checked: values.public_ip, onChange: event => change('public_ip', event.target.checked) }),
      'Assign a public IPv4 address')))
  if (kind === 'ec2.resize') controls.push(field('type', 'New instance type', { ...required, list: SIZES }))
  if (kind === 'ec2.rename') controls.push(field('name', 'Instance name', { ...required, maxLength: 64 }))
  if (kind === 'ec2.security_group') controls.push(
    field('name', 'Security group name', { ...required, maxLength: 64 }),
    field('vpc', 'VPC', { ...required, list: [['', 'Choose a VPC'], ...(extras?.vpcs || []).map(vpc => [vpc.id, vpc.name])] }),
    field('ssh_cidr', 'SSH source (optional)', { wide: true, maxLength: 32, placeholder: '203.0.113.10/32' }))
  if (kind === 'ec2.key_import') controls.push(field('name', 'Key name', { ...required, maxLength: 64 }),
    field('payload', 'OpenSSH public key', { ...required, type: 'textarea', wide: true }))
  if (kind === 'lambda.create') controls.push(
    field('name', 'Function name', { ...required, maxLength: 64, placeholder: 'process-event' }),
    field('runtime', 'Python runtime', { ...required, list: RUNTIMES }),
    field('role', 'Execution role ARN', { ...required, wide: true, maxLength: 255, list: undefined, placeholder: 'arn:aws:iam::…:role/…' }),
    extras?.roles?.length ? h('div', { className: 'field wide', key: 'roles' },
      h('label', { htmlFor: 'aws-existing-role' }, 'Choose an existing role'),
      h('select', { id: 'aws-existing-role', value: extras.roles.some(role => role.arn === values.role) ? values.role : '',
        onChange: event => change('role', event.target.value) }, options([['', 'Select a role'], ...extras.roles.map(role => [role.arn, role.name])]))) : null)
  if (['lambda.create', 'lambda.configure'].includes(kind)) controls.push(
    field('memory', 'Memory (MB)', { ...required, type: 'number', min: 128, max: 10240 }),
    field('timeout', 'Timeout (seconds)', { ...required, type: 'number', min: 1, max: 900 }))
  if (['lambda.create', 'lambda.deploy'].includes(kind)) controls.push(
    field('payload', 'Python handler', { ...required, wide: true, type: 'textarea', spellCheck: false }))
  if (kind === 'lambda.invoke') controls.push(field('payload', 'Test event JSON', { ...required, wide: true, type: 'textarea' }))
  if (kind === 'lambda.role_create') controls.push(field('name', 'IAM role name', { ...required, maxLength: 64 }),
    field('function', 'Lambda function name', { ...required, maxLength: 64 }))
  if (kind === 's3.create' || kind === 's3.connect') controls.push(
    field('bucket', 'Bucket name', { ...required, maxLength: 63, wide: true, placeholder: 'my-team-artifacts' }))
  if (kind === 's3.upload') controls.push(
    h('div', { className: 'field wide', key: 'file' },
      h('label', { htmlFor: 'aws-upload-file' }, 'File'),
      h('input', { type: 'file', id: 'aws-upload-file', required: true, onChange: fileSelected })),
    field('key', 'Object key', { ...required, maxLength: 1024, wide: true, placeholder: 'reports/file.csv' }))

  const details = plan && Object.entries(plan.settings).filter(([key, value]) =>
    value !== '' && !['root_device', 'revision', 'etag'].includes(key)).map(([key, value]) =>
    [LABELS[key] || key, typeof value === 'boolean' ? (value ? 'Yes' : 'No') : value])

  return h('dialog', { ref: dialog, className: 'card dialog', 'aria-labelledby': 'aws-action-title',
    onCancel: event => { event.preventDefault(); if (!busy) close() } },
    h('form', { onSubmit: submit },
      h('div', { className: 'row between' }, h('h2', { id: 'aws-action-title' }, plan ? 'Review ' + title.toLowerCase() : title),
        button('Close', close, 'ghost', busy)),
      h('p', { className: 'sub' }, ACTION_HELP[kind] || 'Apply this change to the selected resource.'),
      extrasError && !plan && h('p', { className: 'notice warn' }, extrasError),
      issue && h('div', { className: 'notice error', role: 'alert' }, issue),
      plan ? h(React.Fragment, null,
        facts([['Workspace', plan.mode === 'live' ? 'Live AWS' : 'Demo'], ['Region', plan.region], ['Account', plan.account], ...details,
          ...(kind === 's3.upload' ? [['File', fileName], ['Size', size(plan.size)]] : [])]),
        ['lambda.create', 'lambda.deploy', 'lambda.invoke'].includes(kind) && h('details', null,
          h('summary', null, kind === 'lambda.invoke' ? 'Test event' : 'Review Python source'), h('pre', null, values.payload)),
        plan.requires_confirmation && h('div', { className: 'field actions' },
          h('label', { htmlFor: 'aws-confirm' }, 'Type ' + plan.target + ' to confirm'),
          h('input', { id: 'aws-confirm', required: true, value: confirmation, onChange: event => setConfirmation(event.target.value) })))
        : h(React.Fragment, null,
          operation.values.id && facts([['Instance', operation.values.id]]),
          operation.values.name && !['ec2.rename'].includes(kind) && facts([['Function', operation.values.name]]),
          operation.values.bucket && facts([['Bucket', operation.values.bucket]]),
          operation.values.key && kind !== 's3.upload' && facts([['Object key', operation.values.key]]),
          controls.length ? h('div', { className: 'fields' }, controls) : null),
      h('div', { className: 'foot' },
        plan && button('Back', () => { setPlan(null); setConfirmation(''); setIssue('') }, '', busy),
        h('button', { type: 'submit', className: 'primary',
          disabled: busy || (!plan && kind === 's3.upload' && !fileName) ||
            (plan?.requires_confirmation && confirmation !== plan.target) },
        busy ? 'Working…' : plan ? title : 'Review action'))))
}

function App({ api, storage }) {
  const [mode, setMode] = useState('demo')
  const [region, setRegion] = useState(() => {
    const saved = storage?.get('region', 'us-east-1')
    return REGIONS.includes(saved) ? saved : 'us-east-1'
  })
  const [tab, setTab] = useState('overview')
  return h('main', { className: 'aws-console' },
    h('style', null, styles),
    h('div', { className: 'frame' },
      h('header', { className: 'header row between' },
        h('div', { className: 'brand' }, h('span', { className: 'brand-icon' }, icon('cloud')),
          h('div', null, h('h1', null, 'AWS Manager'), h('p', { className: 'sub' }, 'Manage compute and storage in Hermes.'))),
        h('div', { className: 'row' },
          h('select', { 'aria-label': 'AWS region', value: region,
            onChange: event => { const value = event.target.value; setRegion(value); storage?.set('region', value) } }, options(REGIONS)),
          h('div', { className: 'row mode', 'aria-label': 'Workspace mode' },
            h('button', { type: 'button', 'aria-pressed': mode === 'demo', onClick: () => setMode('demo') }, 'Demo'),
            h('button', { type: 'button', 'aria-pressed': mode === 'live', onClick: () => setMode('live') }, 'Live AWS')))),
      h(Workspace, { key: mode + ':' + region, api, scope: { mode, region }, tab, setTab })))
}

export default {
  id: 'aws-manager',
  name: 'AWS Manager',
  register(ctx) {
    const api = (path, options = {}) => ctx.rest(path, { timeoutMs: 59000, ...options })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/aws-manager' }, render: () => h(App, { api, storage: ctx.storage }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/aws-manager', label: 'AWS Manager', codicon: 'cloud' } }
    ])
  }
}
