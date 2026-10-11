export type RecordValue = Record<string, string | number | boolean | null>
export type Change = {resource: string; old: RecordValue | null; new: RecordValue | null; kind: string}
export type WorldEvent = {id: string; at: string; changes: Change[]}
export type Message = {id: string; at: string; role: string; type: string; text?: string; changes?: Change[]; status?: string; tool_id?: string; resolution?: string}
export type Approval = {approval_id: string; payload_digest: string; expires_at: string; tool_id: string; payload: {
  goal?: string; start_url?: string; verify?: {record_id: string; conditions: {field: string; op: string; value?: unknown}[]}}}
export type Polling = {min_seconds: number; max_seconds: number; error_max_seconds: number; jitter_percent: number}
export type Watch = {
  task_id: string; kind?: string; label: string; state: string; lifecycle: string; version: number; spec_digest: string; url: string;
  source: {health: string; health_reason: string | null}; records: Record<string, RecordValue>; field: string; field_label: string;
  condition_label: string; condition_met: boolean; last_confirmed_at: string | null; next_check_at: string | null;
  interval_seconds: number; stale: boolean; attention: string | null; pending_approvals: Approval[]; changes: WorldEvent[];
  history: Message[]; approval_expired: boolean; polling_suspended: boolean; polling_reason: string | null;
  session_state?: string;
  next_action_at?: string | null;
  configuration: {extractor: unknown; polling: Polling; spec: {purpose: string; authority: {write_capabilities: string[]; model_data_egress?: string[]}; source: {resource_scope: {on_event?:string; action_goal?:string; action?: {payload: {goal: string}}}}}}
}
export type State = {platform_available?: boolean; items: Watch[]; dialogue: DialogueMessage[]; understanding_available: boolean; coding_available: boolean; workspace: string | null; demo: boolean; demo_url: string | null; runtime_configured: boolean; runtime: {available: boolean; model_configured: boolean}; server_time: string; agent_busy?: boolean; pi?: PiSnapshot | null}
export type Candidate = {selector: string; label: string; value: string; numeric: boolean; context: string}
export type Draft = {task_id: string; spec_digest: string; label: string; condition_label: string; current: RecordValue; source?:{kind:string;target:string;display?:number}; action: {goal: string} | null; data_egress: string[]; url: string; already_met?: boolean; wait_summary?: string}
export type DialogueMessage = {id: string; role: string; text: string; type?: string; at: string; url?: string; draft?: Draft; state?: string; tool?: string; output?: string; diff?: string; rendered_by_stream?: boolean; thinking?: string; arguments?: unknown; images?: PiImage[]; expanded_prompt?: string;computer_action_id?:string;terminal_id?:string;preview_id?:string}
export type Theme = 'light' | 'dark' | 'system'
export type AppSettings = {theme: Theme; locale: string; notify_on_change: boolean; notify_on_approval: boolean; notify_sound: boolean;
  polling_min_seconds: number; polling_max_seconds: number; orb_hue: number | null}
export type SettingsResponse = AppSettings & {_model_configured: boolean; _data_dir: string | null}
export type PiImage = {type:'image'; data:string; mimeType:string}
export type GatewayTest = {id:string; state:'running'|'succeeded'|'failed'|'cancelled'|'interrupted'|'superseded'; current:boolean; message?:string}
export type Gateway = {id:string; provider_id:string; name:string; base_url:string; api:string; model:string; request_format:string; model_options?:Record<string, unknown>; has_credentials:boolean; credential_state?:'available'|'missing'|'unavailable'; connection_test?:GatewayTest; last_test?:{ok:boolean; message:string; latency_ms:number; tested_at:string}}
export type GatewayList = {items:Gateway[]; active_id:string | null; secure_storage:string; revision:number; test_errors?:Record<string,string>; runtime?:{state:'not_started'|'applied'|'pending'|'unavailable'; agentReady?:boolean; message?:string; selectionError?:string; selected?:{provider:string; id:string; profile_id?:string}}}
export type PiModel = {id:string; name:string; provider:string; input:string[]; contextWindow:number; maxTokens:number; reasoning:boolean}
export type PiInteraction = {id:string; channel:string; method:string; kind?:string; type?:string; title?:string; message?:string; cwd?:string; placeholder?:string; prefill?:string; options?:(string | {id:string; label:string; description?:string})[]}
export type PiNotice = {id:string; type?:string; message?:string; url?:string; instructions?:string; verificationUri?:string; userCode?:string; links?:{url:string; label?:string}[]}
export type PiSnapshot = {available:boolean; agent_ready:boolean; status:string; error?:string; revision:number; pending_ui:PiInteraction[]; title?:string;
  runtime_sessions?:{id:string;path?:string;name:string;running:boolean;attention:boolean;loaded:boolean;current:boolean}[];runtime_limit?:number;runtime_busy?:boolean;
  info?:{warning?:string};
  state?:{model?:PiModel; sessionName?:string; sessionId:string; sessionFile?:string; thinkingLevel:string; isStreaming:boolean; isCompacting:boolean; autoCompactionEnabled:boolean; pendingMessageCount:number; steeringMode:string; followUpMode:string} | null;
  stats?:{tokens?:{total:number}; cost?:number; toolCalls?:number; contextUsage?:{tokens:number | null; contextWindow:number; percent:number | null}} | null;
  notices:PiNotice[]; widgets:Record<string,string[]>; statuses:Record<string,string | null>; editor?:{id:string; text:string} | null}
