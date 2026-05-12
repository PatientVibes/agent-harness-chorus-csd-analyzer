# AWD / Chorus Classic Reference

## Field Code Conventions

AWD field codes are exactly 4 characters. Common patterns:

| Prefix/Code | Meaning | Examples |
|------------|---------|----------|
| ACCT | Account/policy number | Primary identifier |
| STAT | Status code | Active/Closed/Pending |
| UNIT | Business unit | Organizational assignment |
| WRKT | Work type | Classification of work item |
| CRDA | Create date | When record was created |
| RCDA | Received date | When item was received |
| PRDA | Process date | When item was processed |
| STDA | Status date | When status last changed |
| DODA | Due/DO date | When item is due |
| EFDA | Effective date | Policy effective date |
| EXDA | Expiration date | Policy expiration |
| CLDA | Close date | When item was closed |
| RECO | Record count | Number of associated records |
| CRNO | Create number | System-assigned sequence |
| OWNE | Owner | Assigned user/queue |
| LOCK | Lock status | Checked-out indicator |
| OBJF | Object flags | System control bits |
| OPTS | Options | Configuration flags |
| OBJI | Object ID | Internal system identifier |
| PRTY | Priority | Processing priority (1-9) |
| SUSP | Suspend code | Reason for suspension |
| COMF | Comments field | Free-text notes (multiline) |
| LOBF | Line of business | Business line classification |
| CLNT | Client | Client/customer identifier |

### Infrastructure Fields

These fields appear on most forms and are managed by the AWD system:
UNIT, WRKT, STAT, CRDA, RECO, CRNO, OWNE, LOCK, OBJF, OPTS, OBJI, PRTY, SUSP, COMF

They are typically read-only or system-populated. Treat them as infrastructure, not business data.

## DLL Hook Patterns

CSD forms reference DLL hooks for field validation, list population, and post-processing. Based on analysis of 4 client DLL implementations (BFDS, NFDS, VKAC, ADP), there are 6 distinct patterns:

| Pattern | Legacy DLL Behavior | Modern Chorus Equivalent | Post-Migration Action |
|---------|--------------------|--------------------------|-----------------------|
| **Field Validation** | Read fields via CsdGetDataValue(), validate cross-field logic, update via CsdSetDataValue() | Custom Rules JS in Screen Designer, or server-side validation | Review validation logic; reimplement critical rules in Custom Rules |
| **List Population** (QUIKLKUP/AWDLIST) | Query host data to populate dropdown values | **Already handled** — domain values from v1 field registry baked into select controls | None — conversion pipeline handles this automatically |
| **Auto-Cloning** | Detect status/type conditions, create cloned work objects via AwdApiCreateObject(), link parent-child relationships | **Workflow/process model** — JSON graph definitions with routing rules | **Human action required:** Design workflow rule in Process Model to replicate cloning behavior |
| **Field Splitting** (SPLITTER) | Split accounts/funds into multiple cases via dialog UI | **Process model** action or custom JS dialog | **Human action required:** Design split workflow in Process Model |
| **Suspension/Post-Processing** | Auto-suspend/activate cases based on queue movement, status changes | **Workflow routing rules** — server-side queue management | **Human action required:** Configure routing rules to replicate suspension logic |
| **Document Handling** | Retrieve images/documents when trigger flags set (e.g., IMGF="Y") | **Built-in** — Related Attachments widget, PLUploader area control | Map trigger fields to PLUploader configuration |

### DLL Function Signature

All DLLs share a common entry point signature:
```
BOOL EXPENTRY fnName(HWND hwndCSD, PAWDLOBARRAY pAwdLobArray, PSZ pszFocusName)
```
- `PAWDLOBARRAY` contains field name/value pairs (4-char code + 75-char value)
- `pszFocusName` is the field code that triggered the DLL call

### How to Report DLL Hooks in Analysis

When a form has DLL hooks, classify each hook by pattern and produce **human-actionable guidance**:

1. **List population hooks** → "Automatically handled by conversion — domain values come from field registry"
2. **Validation hooks** → "Review validation logic for reimplementation in Screen Designer Custom Rules"
3. **Cloning/splitting/suspension hooks** → "**Post-migration work required:** This DLL implements [pattern] logic that must be designed as a workflow rule in the Chorus Process Model. The form itself converts cleanly, but the [cloning/splitting/suspension] behavior will not carry over without a corresponding process model change."
4. **Document handling hooks** → "Map to PLUploader area control or Related Attachments widget"

**Critical: always distinguish between what the form conversion handles (screen layout, fields, controls) and what requires separate process model work (workflow rules, cloning, routing).**

## Field Format Types

AWD uses single-character format type codes:

| Code | Type | Description | Mask Characters |
|------|------|-------------|-----------------|
| A | Alphabetic | Letters only | ! = uppercase letter |
| X | Alphanumeric | Letters + digits | X = any alphanumeric |
| 9 | Numeric text | Digits stored as text | 9 = digit |
| N | Numeric | True number with decimals | 9 = digit, . = decimal |
| C | Currency | Formatted money amount | 9 = digit, . = decimal, , = separator |
| D | Date | Date value | MM/DD/YYYY or YYYYMMDD |
| S | Timestamp | Date + time | YYYY-MM-DD HH:MM:SS |
| T | Time | Time only | HH:MM:SS |

### Mask Characters
- `!` = uppercase alphabetic
- `X` = alphanumeric (letter or digit)
- `9` = digit
- `#` = digit, space, or sign
- `.` = decimal point
- `,` = thousands separator

## Form Types

| Type | Purpose | Typical Fields |
|------|---------|---------------|
| work | Active work items — claims, applications, requests | Business fields + all infrastructure fields |
| source | Origin/source tracking — where the work came from | Source-specific fields (SRST, SRTP) + infrastructure |
| folder | Grouping container — organizes related work items | Minimal fields, mostly infrastructure |
| lookup | Reference/search tables — static data lookup | Key fields for search + display columns |

## Migration Risk Patterns

When analyzing CSD forms for Chorus migration, flag these:
- **No validation on required fields** — field marked required but no DLL hook or mask
- **DLL hooks requiring process model work** — cloning, splitting, suspension hooks need workflow rules designed separately from the form migration. Always note: "Form converts cleanly; post-migration process model work required for [specific behavior]."
- **Inconsistent field usage** — same field code used as input on one form but combobox on another
- **Hardcoded masks** — masks that assume a specific format that may change
- **Missing labels** — fields without labels (label pairing failed) need manual review
- **Cross-field validation DLLs** — validation logic that reads/writes multiple fields needs reimplementation in Custom Rules JS

### Migration Handoff Notes

For every form with DLL hooks, the analysis MUST produce a **human-readable handoff section** that separates:
1. **What the automated conversion handles** — screen layout, field types, control mapping, domain values
2. **What requires manual follow-up** — process model rules (cloning, splitting, suspension), custom validation JS, document handling configuration

This ensures the migration team knows exactly what's done and what's left after the form is imported into Screen Designer.
