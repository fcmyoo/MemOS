REORGANIZE_PROMPT = """You are a memory clustering and summarization expert.

Given the following child memory items:

{memory_items_text}

Please perform:
1. Identify information that reflects user's experiences, beliefs, concerns, decisions, plans, or reactions — including meaningful input from assistant that user acknowledged or responded to.
2. Resolve all time, person, and event references clearly:
   - Convert relative time expressions (e.g., “yesterday,” “next Friday”) into absolute dates using the message timestamp if possible.
   - Clearly distinguish between event time and message time.
   - If uncertainty exists, state it explicitly (e.g., “around June 2025,” “exact date unclear”).
   - Include specific locations if mentioned.
   - Resolve all pronouns, aliases, and ambiguous references into full names or identities.
   - Disambiguate people with the same name if applicable.
3. Always write from a third-person perspective, referring to user as
"The user" or by name if name mentioned, rather than using first-person ("I", "me", "my").
For example, write "The user felt exhausted..." instead of "I felt exhausted...".
4. Do not omit any information that user is likely to remember.
   - Include all key experiences, thoughts, emotional responses, and plans — even if they seem minor.
   - Prioritize completeness and fidelity over conciseness.
   - Do not generalize or skip details that could be personally meaningful to user.
5. Summarize all child memory items into one memory item.

Language rules:
- The `key`, `value`, `tags`, `summary` fields must match the mostly used language of the input memory items.  **如果输入是中文，请输出中文**
- Keep `memory_type` in English.

Return valid JSON:
{
  "key": <string, a concise title of the `value` field>,
  "memory_type": <string, Either "LongTermMemory" or "UserMemory">,
  "value": <A detailed, self-contained, and unambiguous memory statement, only contain detailed, unaltered information extracted and consolidated from the input `value` fields, do not include summary content — written in English if the input memory items are in English, or in Chinese if the input is in Chinese>,
  "tags": <A list of relevant thematic keywords (e.g., ["deadline", "team", "planning"])>,
  "summary": <a natural paragraph summarizing the above memories from user's perspective, only contain information from the input `summary` fields, 120–200 words, same language as the input>
}

"""

POLICY_INDUCTION_PROMPT = """你是一名行为规则归纳专家（policy induction），负责从同一主题的多条记忆事件（episode）中提炼可复用的行为规则，而不是复述这些事件发生了什么。

以下是同一主题簇内的记忆条目（每条包含 key/value/summary）：

{memory_items_text}

任务要求：
1. 只输出**可复用的条件化行为规则**（policy），形如"在场景 X 下，采取行为 A 优于/应当 B"——不要只是把输入事件换个说法复述一遍。
2. 规则必须能从至少两条独立证据中归纳出来，具备跨事件的稳定性，而非单次偶然行为。
3. 明确规则生效的场景/条件（时间、任务类型、触发条件等），以及该场景下应采取的行动，和为何这个行动更优（对比、结果、教训）。
4. 如实自评这条规则的置信度/收益（gain_self_eval，0.0-1.0）：
   - 证据越多、越一致、规则越可执行 → 越接近 1.0
   - 证据单薄、规则含糊、更像是巧合或一次性偏好 → 越接近 0.0
5. 如果这批记忆无法归纳出任何可复用的行为规则（例如内容彼此无关、只是时间相邻、或都是一次性事件），**不要勉强编造规则**，直接输出 {{"no_policy": true, "reason": "<说明无法归纳的原因>"}}。

语言规则：
- 全部输出使用中文（输入是中文）。

请返回合法 JSON（能归纳出规则时）：
{{
  "policy_key": <string，规则的简短名称，如"调试卡住时优先查日志而非猜测">,
  "policy_value": <string，条件化行为规则本身，说明"在场景 X 下，采取 A 优于 B"，必须是可执行的规则陈述，不得是事件复述>,
  "evidence_count": <int，支持该规则的独立证据条数（本次输入中实际支持规则的条目数，而非簇总条数）>,
  "gain_self_eval": <float，0.0-1.0，对该规则可信度/收益的自评>,
  "tags": <字符串数组，规则相关的主题关键词>,
  "summary": <string，支持该规则的证据概要，说明这些证据如何共同指向该规则，而非逐条复述事件>
}}

无法归纳规则时，只返回：
{{"no_policy": true, "reason": <string，说明原因>}}
"""

WORLD_MODEL_PROMPT = """你是一名用户画像/世界模型归纳专家（world model induction），负责从同一主题下的多条行为规则（L2 policy）中提炼跨场景仍然稳定的规律或用户画像，而不是复述某一条具体规则。

以下是同一主题簇内的 L2 policy 条目（每条包含 policy_key/policy_value/summary）：

{memory_items_text}

任务要求：
1. 只输出**跨场景仍然成立的稳定规律或用户画像**（world model），形如"用户是……类型的使用者"或"用户的工作流遵循……模式"——不要只是把某一条 policy 换个说法复述一遍。
2. 该画像/规律必须能从至少两条独立的 policy 中共同归纳出来，代表这些 policy 背后共享的更高层特征，而不是其中一条的延伸或简单堆叠。
3. 明确这个画像/规律适用的场景范围（哪些场景下用户表现出这个特征），以及支撑它的 policy 证据。
4. 如实自评这条 world model 的置信度/收益（gain_self_eval，0.0-1.0）：
   - 支撑证据（policy）越多、彼此越一致、画像越可执行 → 越接近 1.0
   - 证据单薄、画像含糊、或更像是巧合而非稳定规律 → 越接近 0.0
5. 如果这批 policy 彼此没有共同主题（各自独立、无法归纳出更高层的用户画像/规律），**不要勉强编造**，直接输出 {{"no_world_model": true, "reason": "<说明无法归纳的原因>"}}。

语言规则：
- 全部输出使用中文（输入是中文）。

请返回合法 JSON（能归纳出 world model 时）：
{{
  "world_key": <string，画像/规律的简短名称，如"偏好日志优先的调试型使用者">,
  "world_value": <string，跨场景稳定规律/用户画像本身，说明"用户是……类型的使用者"或"用户的工作流遵循……模式"，必须是跨场景仍然成立的稳定陈述，不得是单条 policy 复述>,
  "evidence_count": <int，支持该 world model 的独立 policy 证据条数（本次输入中实际支持该画像的条目数，而非簇总条数）>,
  "gain_self_eval": <float，0.0-1.0，对该 world model 可信度/收益的自评>,
  "tags": <字符串数组，world model 相关的主题关键词>,
  "summary": <string，支持该 world model 的 policy 证据概要，说明这些 policy 如何共同指向该画像/规律，而非逐条复述>
}}

无法归纳 world model 时，只返回：
{{"no_world_model": true, "reason": <string，说明原因>}}
"""

DOC_REORGANIZE_PROMPT = """You are a document summarization and knowledge extraction expert.

Given the following summarized document items:

{memory_items_text}

Please perform:
1. Identify key information that reflects factual content, insights, decisions, or implications from the documents — including any notable themes, conclusions, or data points.
2. Resolve all time, person, location, and event references clearly:
   - Convert relative time expressions (e.g., “last year,” “next quarter”) into absolute dates if context allows.
   - Clearly distinguish between event time and document time.
   - If uncertainty exists, state it explicitly (e.g., “around 2024,” “exact date unclear”).
   - Include specific locations if mentioned.
   - Resolve all pronouns, aliases, and ambiguous references into full names or identities.
   - Disambiguate entities with the same name if applicable.
3. Always write from a third-person perspective, referring to the subject or content clearly rather than using first-person ("I", "me", "my").
4. Do not omit any information that is likely to be important or memorable from the document summaries.
   - Include all key facts, insights, emotional tones, and plans — even if they seem minor.
   - Prioritize completeness and fidelity over conciseness.
   - Do not generalize or skip details that could be contextually meaningful.
5. Summarize all document summaries into one integrated memory item.

Language rules:
- The `key`, `value`, `tags`, `summary` fields must match the mostly used language of the input document summaries.  **如果输入是中文，请输出中文**
- Keep `memory_type` in English.

Return valid JSON:
{
  "key": <string, a concise title of the `value` field>,
  "memory_type": "LongTermMemory",
  "value": <A detailed, self-contained, and unambiguous memory statement, only contain detailed, unaltered information extracted and consolidated from the input `value` fields, do not include summary content — written in English if the input memory items are in English, or in Chinese if the input is in Chinese>,
  "tags": <A list of relevant thematic keywords (e.g., ["deadline", "team", "planning"])>,
  "summary": <a natural paragraph summarizing the above memories from user's perspective, only contain information from the input `summary` fields, 120–200 words, same language as the input>
}

"""


LOCAL_SUBCLUSTER_PROMPT = """You are a memory organization expert.

You are given a cluster of memory items, each with an ID and content.
Your task is to divide these into smaller, semantically meaningful sub-clusters.

Instructions:
- Identify natural topics by analyzing common time, place, people, and event elements.
- Each sub-cluster must reflect a coherent theme that helps retrieval.
- Each sub-cluster should have 2–10 items. Discard singletons.
- Each item ID must appear in exactly one sub-cluster or be discarded. No duplicates are allowed.
- All IDs in the output must be from the provided Memory items.
- Return strictly valid JSON only.

Example: If you have items about a project across multiple phases, group them by milestone, team, or event.

Language rules:
- The `key` fields must match the mostly used language of the clustered memories. **如果输入是中文，请输出中文**

Return valid JSON:
{
  "clusters": [
    {
      "ids": ["<id1>", "<id2>", ...],
      "key": "<string, a unique, concise memory title>"
    },
    ...
  ]
}

Memory items:
{joined_scene}
"""

PAIRWISE_RELATION_PROMPT = """
You are a reasoning assistant.

Given two memory units:
- Node 1: "{node1}"
- Node 2: "{node2}"

Your task:
- Determine their relationship ONLY if it reveals NEW usable reasoning or retrieval knowledge that is NOT already explicit in either unit.
- Focus on whether combining them adds new temporal, causal, conditional, or conflict information.

Valid options:
- CAUSE: One clearly leads to the other.
- CONDITION: One happens only if the other condition holds.
- RELATE: They are semantically related by shared people, time, place, or event, but neither causes the other.
- CONFLICT: They logically contradict each other.
- NONE: No clear useful connection.

Example:
- Node 1: "The marketing campaign ended in June."
- Node 2: "Product sales dropped in July."
Answer: CAUSE

Another Example:
- Node 1: "The conference was postponed to August due to the venue being unavailable."
- Node 2: "The venue was booked for a wedding in August."
Answer: CONFLICT

Always respond with ONE word, no matter what language is for the input nodes: [CAUSE | CONDITION | RELATE | CONFLICT | NONE]
"""

INFER_FACT_PROMPT = """
You are an inference expert.

Source Memory: "{source}"
Target Memory: "{target}"

They are connected by a {relation_type} relation.
Derive ONE new factual statement that clearly combines them in a way that is NOT a trivial restatement.

Requirements:
- Include relevant time, place, people, and event details if available.
- If the inference is a logical guess, explicitly use phrases like "It can be inferred that...".

Example:
Source: "John missed the team meeting on Monday."
Target: "Important project deadlines were discussed in that meeting."
Relation: CAUSE
Inference: "It can be inferred that John may not know the new project deadlines."

If there is NO new useful fact that combines them, reply exactly: "None"
"""

AGGREGATE_PROMPT = """
You are a concept summarization assistant.

Below is a list of memory items:
{joined}

Your task:
- Identify if they can be meaningfully grouped under a new, higher-level concept that clarifies their shared time, place, people, or event context.
- Do NOT aggregate if the overlap is trivial or obvious from each unit alone.
- If the summary involves any plausible interpretation, explicitly note it (e.g., "This suggests...").

Example:
Input Memories:
- "Mary organized the 2023 sustainability summit in Berlin."
- "Mary presented a keynote on renewable energy at the same summit."

Language rules:
- The `key`, `value`, `tags`, `background` fields must match the language of the input.

Good Aggregate:
{
  "key": "Mary's Sustainability Summit Role",
  "value": "Mary organized and spoke at the 2023 sustainability summit in Berlin, highlighting renewable energy initiatives.",
  "tags": ["Mary", "summit", "Berlin", "2023"],
  "background": "Combined from multiple memories about Mary's activities at the summit."
}

If you find NO useful higher-level concept, reply exactly: "None".
"""

REDUNDANCY_MERGE_PROMPT = """You are given two pieces of text joined by the marker `⟵MERGED⟶`. Please carefully read both sides of the merged text. Your task is to summarize and consolidate all the factual details from both sides into a single, coherent text, without omitting any information. You must include every distinct detail mentioned in either text. Do not provide any explanation or analysis — only return the merged summary. Don't use pronouns or subjective language, just the facts as they are presented.\n{merged_text}"""


MEMORY_RELATION_DETECTOR_PROMPT = """You are a memory relationship analyzer.
You are given two plaintext statements. Determine the relationship between them. Classify the relationship into one of the following categories:

contradictory: The two statements describe the same event or related aspects of it but contain factually conflicting details.
redundant: The two statements describe essentially the same event or information with significant overlap in content and details, conveying the same core information (even if worded differently).
independent: The two statements are either about different events/topics (unrelated) OR describe different, non-overlapping aspects or perspectives of the same event without conflict (complementary). In both sub-cases, they provide distinct information without contradiction.
Respond only with one of the three labels: contradictory, redundant, or independent.
Do not provide any explanation or additional text.

Statement 1: {statement_1}
Statement 2: {statement_2}
"""


MEMORY_RELATION_RESOLVER_PROMPT = """You are a memory fusion expert. You are given two statements and their associated metadata. The statements have been identified as {relation}. Your task is to analyze them carefully, considering the metadata (such as time, source, or confidence if available), and produce a single, coherent, and comprehensive statement that best represents the combined information.

If the statements are redundant, merge them by preserving all unique details and removing duplication, forming a richer, consolidated version.
If the statements are contradictory, attempt to resolve the conflict by prioritizing more recent information, higher-confidence data, or logically reconciling the differences based on context. If the contradiction is fundamental and cannot be logically resolved, output <answer>No</answer>.
Do not include any explanations, reasoning, or extra text. Only output the final result enclosed in <answer></answer> tags.
Strive to retain as much factual content as possible, especially time-specific details.
Use objective language and avoid pronouns.
Output Example 1 (unresolvable conflict):
<answer>No</answer>

Output Example 2 (successful fusion):
<answer>The meeting took place on 2023-10-05 at 14:00 in the main conference room, as confirmed by the updated schedule, and included a presentation on project milestones followed by a Q&A session.</answer>

Now, reconcile the following two statements:
Relation Type: {relation}
Statement 1: {statement_1}
Metadata 1: {metadata_1}
Statement 2: {statement_2}
Metadata 2: {metadata_2}
"""
