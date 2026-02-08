# ClientCloak

Bidirectional document sanitization for safe contract review with cloud AI tools or in public places like coworking spaces, airplanes, and coffee shops.

## The Problem

Attorneys want to use AI tools (Claude, ChatGPT, Gemini, etc.) from foundational LLM cloud providers for assistance with client work. 

Sending documents with client names, deal terms, and personal data to the cloud without enterprise grade security commitments may create risks that an attorney could violate ethical obligations to preserve client confidentiality or otherwise cause reputational harm. 

Attorneys and clients may not be comfortable with confidential information being sent via the cloud to those cloud providers in light of pending litigation that might require litigation holds and discovery.

Even if you or your law firm have access to a include no training on user input, zero data retention, clear confidentiality obligations, along with a data processing addendum.

California COPRAC guidance (Nov 2023) and ABA Formal Opinion 512 state that attorneys should anonymize client information before using generative AI tools.

## The Solution

ClientCloak sanitizes your documents before they go to AI, then restores the original information after AI analysis.

```
Original Doc -> [Cloak] -> Sanitized Doc -> AI Tool -> Redlined Doc -> [Uncloak] -> Final Doc
                   |                                                       ^
               Mapping File -----------------------------------------------'
```

## Features

- **Party Name Replacement**: Replace "Acme Corp" -> "Customer", "BigCo LLC" -> "Vendor" (customizable labels)
- **Party Aliases**: Handle parenthetical definitions like `Acme Corp. ("Acme")` — both forms get replaced
- **Security Scanning**: Detects hidden text and prompt injection attempts from opposing counsel
- **Metadata Removal**: Strips author names, revision history, timestamps
- **Comment Handling**: Strip, anonymize, or fully sanitize Word comments
- **Bidirectional**: Cloak before AI, uncloak after — mapping file keeps track
- **Format Preservation**: Maintains document formatting, track changes, and comments
- **100% Local**: All processing on your machine. No data sent anywhere.

## Installation

```bash
git clone https://github.com/YOURUSERNAME/clientcloak.git
cd clientcloak
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

## Quick Start

### Using the UI

```bash
clientcloak-ui
```

Opens a local web interface in your browser. No data leaves your machine.

### Using the CLI

```bash
# Cloak a document
clientcloak cloak contract.docx --party-a "Acme Corp" --party-b "BigCo LLC" --labels customer/vendor

# With aliases for parenthetical definitions
clientcloak cloak contract.docx --party-a "Acme Corp" --party-b "BigCo LLC" --labels licensor/licensee --alias-a "Acme=Licensor"

# Send contract_cloaked.docx to your AI tool...

# Uncloak the redlined result
clientcloak uncloak redlined.docx --mapping contract_mapping.json

# Security scan only
clientcloak scan contract.docx

# Inspect metadata and comments
clientcloak inspect contract.docx
```

## Workflow

1. **Cloak**: Upload your contract, specify party names, review what will be protected
2. **Download**: Get the sanitized document + mapping file (keep mapping file safe!)
3. **AI Review**: Send sanitized document to Claude, ChatGPT, Gemini, or any AI tool
4. **Uncloak**: Upload the AI's redlined output + your mapping file
5. **Done**: Get the final document with original names restored

## Security Note

The mapping file contains the link between placeholders and real client data. Keep it secure and never share it.

## License

MIT
