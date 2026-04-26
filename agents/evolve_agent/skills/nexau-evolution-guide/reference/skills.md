# Skills

Skills are reusable capabilities that can be loaded and used by agents. NexAU supports two types of skills:

1. **Folder-based skills**: Skills defined in dedicated folders with a `SKILL.md` file
2. **Tool-based skills**: Regular tools marked as skills using the `as_skill` parameter

Both types of skills are automatically registered and can be loaded dynamically by agents using the `LoadSkill` tool.

## Table of Contents

- [Folder-Based Skills](#folder-based-skills)
- [Tool-Based Skills](#tool-based-skills)
- [Combining Both Types](#combining-both-types)
- [How Skills Work](#how-skills-work)
- [Best Practices](#best-practices)

---

## Folder-Based Skills

Folder-based skills are self-contained capabilities stored in dedicated directories. Each skill folder must contain a `SKILL.md` file with YAML frontmatter.

### Creating a Folder-Based Skill

#### Step 1: Create the Skill Folder Structure

```
my_project/
├── skills/
│   ├── data-analysis/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   │   └── analyzer.py
│   │   └── templates/
│   │       └── report_template.md
│   └── web-scraping/
│       ├── SKILL.md
│       └── utils.py
```

#### Step 2: Write the SKILL.md File

The `SKILL.md` file must start with YAML frontmatter containing `name` and `description` fields:

```markdown
---
name: data-analysis
description: Advanced data analysis and visualization capabilities
---

# Data Analysis Skill

This skill provides comprehensive data analysis capabilities including:

## Features

- Statistical analysis
- Data visualization
- Report generation
- Export to multiple formats

## Usage

To use this skill, you can access the analysis scripts in the `scripts/` directory:

```python
# Example usage
from scripts.analyzer import analyze_data
results = analyze_data(data)
```

## Files

- `scripts/analyzer.py` - Main analysis functions
- `templates/report_template.md` - Report template

## Requirements

- pandas >= 1.5.0
- matplotlib >= 3.5.0
```

#### Step 3: Configure Your Agent

**Using Python:**

```python
from nexau import Agent, AgentConfig

agent = Agent(
    config=AgentConfig(
        name="data_analyst",
        llm_config={"model": "gpt-4o-mini"},
        skills=["skills/data-analysis", "skills/web-scraping"],
    )
)
```

**Using YAML:**

```yaml
type: agent
name: data_analyst
llm_config:
  model: gpt-4o-mini
skills:
  - skills/data-analysis
  - skills/web-scraping
```

### SKILL.md Format Requirements

The `SKILL.md` file **must**:
- Start with `---` (YAML frontmatter delimiter)
- Include a `name` field
- Include a `description` field
- End the frontmatter with `---`
- Have content after the frontmatter (the detailed skill documentation)

**Example:**

```markdown
---
name: my-skill
description: Brief one-line description of what this skill does
---

# Detailed Documentation

Everything after the closing `---` becomes the skill's detailed documentation,
which agents can access using the LoadSkill tool.
```

---

## Tool-Based Skills

Tool-based skills are regular tools that are marked as skills. This is useful when you want to expose a tool's functionality as a discoverable skill.

### Creating a Tool-Based Skill

#### Step 1: Create a Tool with `as_skill=True`

```python
from nexau import Tool

# Create a tool that's also a skill
code_generator = Tool(
    name="generate_code",
    description="Generates code based on specifications",
    input_schema={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "Programming language"},
            "specification": {"type": "string", "description": "Code specification"}
        },
        "required": ["language", "specification"]
    },
    implementation=generate_code_implementation,
    as_skill=True,  # Mark this tool as a skill
    skill_description="Code generation skill for multiple programming languages including Python, JavaScript, Java, and C++"
)
```

**Important:** When `as_skill=True`, you **must** provide a `skill_description` parameter. This is the brief description that appears in the skill registry.

#### Step 2: Add the Tool to Your Agent

```python
from nexau import Agent, AgentConfig

agent = Agent(
    config=AgentConfig(
        name="coding_assistant",
        llm_config={"model": "gpt-4o-mini"},
        tools=[code_generator],
    )
)
```

The agent will automatically:
1. Register the tool as a skill
2. Add the `LoadSkill` tool to access skill details
3. Include the skill in the skill registry

### Tool-Based Skills in YAML

**Tool Definition (generate_code.tool.yaml):**

```yaml
type: tool
name: generate_code
description: Generates code based on specifications
input_schema:
  type: object
  properties:
    language:
      type: string
      description: Programming language
    specification:
      type: string
      description: Code specification
  required:
    - language
    - specification
as_skill: true
skill_description: Code generation skill for multiple programming languages
```

### Tool-Based Skills by `tool_call_mode`

Tool-based skills behave slightly differently depending on the active tool calling strategy:

| `tool_call_mode` | What the model sees initially | When `LoadSkill` is needed |
| --- | --- | --- |
| `xml` | Only the tool's `skill_description` in the prompt skill registry | When the model needs the full tool instructions |
| `openai` | The tool's `skill_description` plus the full JSON Schema in the structured tool definition | When the model needs the full workflow-level `description` |
| `anthropic` | The tool's `skill_description` plus the full JSON Schema in the structured tool definition | When the model needs the full workflow-level `description` |

This means `description` and `skill_description` now serve different purposes for tool-based skills:

- `skill_description`: short discovery text shown up front
- `description`: detailed operational guidance returned through `LoadSkill`

---

## Combining Both Types

You can use both folder-based and tool-based skills together in the same agent:

```python
from nexau import Agent, AgentConfig, Tool

# Define a tool-based skill
web_search = Tool(
    name="web_search",
    description="Search the web for information",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"}
        },
        "required": ["query"]
    },
    implementation=search_implementation,
    as_skill=True,
    skill_description="Web search capability with advanced filtering"
)

# Create agent with both types
agent = Agent(
    config=AgentConfig(
        name="research_assistant",
        llm_config={"model": "gpt-4o-mini"},
        skills=[
            "skills/data-analysis",    # Folder-based skill
            "skills/report-writing"    # Folder-based skill
        ],
        tools=[
            web_search  # Tool-based skill
        ],
    )
)
```

**YAML Configuration:**

```yaml
type: agent
name: research_assistant
llm_config:
  model: gpt-4o-mini

# Folder-based skills
skills:
  - skills/data-analysis
  - skills/report-writing

# Tool-based skills (and regular tools)
tools:
  - name: web_search
    yaml_path: tools/web_search.tool.yaml
    binding: my_module:search_implementation
```

---

## How Skills Work

### Automatic Skill Registration

When you create an agent with skills or tool-based skills:

1. **Skill Registry**: All skills are registered in the agent's global storage under `skill_registry`
2. **LoadSkill Tool**: A special `LoadSkill` tool is automatically added to the agent
3. **Skill Discovery**: The agent can see brief descriptions of all available skills

### Using Skills at Runtime

The agent can load detailed skill information using the `LoadSkill` tool.

- In `xml` mode, this appears as an XML tool call such as:

```xml
<tool_use>
  <tool_name>LoadSkill</tool_name>
  <parameter>
    <skill_name>data-analysis</skill_name>
  </parameter>
</tool_use>
```

- In `openai` / `anthropic` modes, the model invokes `LoadSkill` through the provider's native structured tool-calling interface using the same `skill_name` argument.

**Response:**

```
Found the skill details of `data-analysis`.
Note that the paths mentioned in skill description are relative to the skill folder.

<SkillDetails>
<SkillName>data-analysis</SkillName>
<SkillFolder>/path/to/skills/data-analysis</SkillFolder>
<SkillDescription>Advanced data analysis and visualization capabilities</SkillDescription>
<SkillDetail>
# Data Analysis Skill

This skill provides comprehensive data analysis capabilities...
(full content from SKILL.md)
</SkillDetail>
</SkillDetails>
```

For tool-based skills in `openai` / `anthropic` modes, the `SkillDetail` body contains the tool's full `description` and guidance for native structured tool calling rather than XML-only usage examples.

### System Prompt Integration

Skills appear in the agent's prompt/runtime metadata like this:

```text
## Available Skills

<Skills>
<SkillBrief>
Skill Name: data-analysis
Skill Folder: /path/to/skills/data-analysis
Skill Brief Description: Advanced data analysis and visualization capabilities

</SkillBrief>
<SkillBrief>
Skill Name: web_search
Skill Brief Description: Web search capability with advanced filtering

</SkillBrief>
</Skills>

You can use the LoadSkill tool to get detailed information about any skill.
```

Notes:

- In `xml` mode, this brief registry is injected into the system prompt.
- In `openai` / `anthropic` modes, folder-based skills still appear through the skill registry, while tool-based skills expose the same brief text through their structured tool definitions (`skill_description`) and use `LoadSkill` for the full detail.

---

## Best Practices

### 1. Folder-Based Skills

✅ **Do:**
- Keep skills self-contained with all necessary files in the skill folder
- Write comprehensive documentation in `SKILL.md`
- Use relative paths when referring to files within the skill
- Include examples and usage instructions
- Document dependencies and requirements

❌ **Don't:**
- Don't rely on files outside the skill folder
- Don't use absolute paths in documentation
- Don't make skills too broad (keep them focused)

### 2. Tool-Based Skills

✅ **Do:**
- Always provide a meaningful `skill_description`
- Use tool-based skills for capabilities that don't need extensive documentation
- Make the skill description concise but informative
- Keep the regular `description` field for the full operational guidance loaded through `LoadSkill`
- Treat `skill_description` as discovery text and `description` as detailed instructions, especially in `openai` / `anthropic` modes

❌ **Don't:**
- Don't set `as_skill=True` without providing `skill_description`
- Don't use empty strings for `skill_description`
- Don't duplicate all of `description` into `skill_description`
- Don't assume structured tool calling models need the full tool description up front

### 3. Choosing Between Types

**Use Folder-Based Skills when:**
- The skill requires extensive documentation
- The skill includes multiple files (scripts, templates, data)
- The skill needs to be version-controlled separately
- The skill will be shared across multiple projects

**Use Tool-Based Skills when:**
- The skill is a single, well-defined capability
- The tool's description is sufficient documentation
- You want quick discoverability of tool capabilities
- The implementation is already a tool

### 4. Organizing Skills

**Good Structure:**

```
project/
├── skills/
│   ├── algorithmic-art/      # Complex skill with multiple files
│   │   ├── SKILL.md
│   │   ├── templates/
│   │   └── examples/
│   └── document-processing/  # Another complex skill
│       ├── SKILL.md
│       └── processors/
├── tools/
│   ├── web_search.tool.yaml  # Simple tool-based skill
│   └── calculator.tool.yaml  # Regular tool (not a skill)
└── agent.yaml
```

### 5. Skill Descriptions

Check [Claude Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/best-practices) for how to write a good skill.

**Good Skill Description (brief):**
```
"Advanced data visualization with support for interactive charts, graphs, and dashboards"
```

**Bad Skill Description (too verbose):**
```
"This skill provides functionality for creating visualizations including but not limited to..."
(continues for several paragraphs)
```

**Good Detailed Documentation (in SKILL.md):**
```markdown
---
name: data-viz
description: Advanced data visualization capabilities
---

# Data Visualization Skill

## Overview
(comprehensive documentation here)

## Examples
(code examples)

## API Reference
(detailed API docs)
```

---

## Examples

### Example 1: Research Assistant with Multiple Skills

```python
from nexau import Agent, AgentConfig, Tool, Skill

# Tool-based skill for web search
web_search_tool = Tool(
    name="web_search",
    description="Search the web",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"}
        }
    },
    implementation=search_impl,
    as_skill=True,
    skill_description="Web search with filtering and ranking"
)

# Create agent with both skill types
agent = Agent(
    config=AgentConfig(
        name="researcher",
        llm_config={"model": "gpt-4o-mini"},
        skills=[
            Skill.from_folder("skills/academic-research"),  # Folder skill
            Skill.from_folder("skills/citation-manager")    # Folder skill
        ],
        tools=[
            web_search_tool  # Tool skill
        ],
    )
)

# Agent can now discover and use all three skills
response = agent.run("Research the latest developments in quantum computing")
```

### Example 2: YAML Configuration with Skills

**agent.yaml:**

```yaml
type: agent
name: creative_assistant
llm_config:
  model: gpt-4o-mini
  temperature: 0.7

# Folder-based skills
skills:
  - skills/algorithmic-art
  - skills/story-generation
  - skills/music-composition

# Tools (some as skills)
tools:
  - name: image_editor
    yaml_path: tools/image_editor.tool.yaml
    binding: image_tools:edit_image
    # This tool has as_skill: true and skill_description in its YAML

  - name: file_manager
    yaml_path: tools/file_manager.tool.yaml
    binding: file_tools:manage_files
    # This is a regular tool (as_skill: false or not specified)
```

## Summary

- **Folder-based skills**: Use for complex, multi-file capabilities with extensive documentation
- **Tool-based skills**: Use for simple, well-defined capabilities that are already tools
- **Both can coexist**: Use them together in the same agent for maximum flexibility
- **LoadSkill tool**: Automatically added to access detailed skill information
- **Skill descriptions**: Keep brief for discovery, detailed for documentation

Skills provide a powerful way to organize and discover agent capabilities, making your agents more maintainable and easier to understand.
