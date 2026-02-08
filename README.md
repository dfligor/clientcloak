# ClientCloak

Bidirectional document sanitization for safe contract review with cloud AI tools or in public places like coworking spaces, airplanes, and coffee shops.

## The Problem

Attorneys want to use AI tools (Claude, ChatGPT, Gemini, etc.) from foundational LLM cloud providers for assistance with client work. They may also want to work from coworking spaces and other public places where someone might see what they are working on.

The California State Bar's Committee on Professional Responsibility and Conduct published "Practical Guidance on the Responsible Use of Generative AI in the Practice of Law" in November 2023. The key takeaway was that lawyers must never input confidential client information into any generative AI tool without adequate protections. Source: https://www.calbar.ca.gov/Portals/0/documents/ethics/Generative-AI-Practical-Guidance.pdf.

ABA Formal Opinion 512 is the ABA's first ethics opinion on lawyers' use of generative AI. It was issued in July 2024. According to the opinion, Model Rule 1.6 (confidentiality) applies to GenAI tools. Before inputting any information into a GenAI tool, lawyers must evaluate the risk that client information will be disclosed to or accessed by others. Lawyers should review the terms and policies of any GenAI tool to understand who has access to inputted data, and should obtain informed consent from clients before using their confidential information with these tools. Boilerplate consent in engagement letters is not sufficient. Source: https://www.americanbar.org/content/dam/aba/administrative/professional_responsibility/ethics-opinions/aba-formal-opinion-512.pdf

Sending documents with client names, deal terms, and personal data to the cloud without enterprise grade security commitments may create risks that an attorney could violate ethical obligations to preserve client confidentiality or otherwise cause reputational harm. Even with such commitments, attorneys and clients may not be comfortable with confidential information being sent via the cloud to those cloud providers in light of pending litigation that might require litigation holds and discovery.

Even if you or your law firm have access to an AI tool with an enterprise license that includes no training on user input, zero data retention, clear confidentiality obligations, along with a data processing addendum, you may want to experiment with free or low cost AI tools.

Finally, before sharing something an attorney worked on with other attorneys and before reusing anything as a template, client-specific information should be removed.

## The Solution

ClientCloak sanitizes your documents locally, replacing confidential names with generic placeholders. A mapping file lets you restore the originals whenever you need them back.

```
Original Doc -> [Cloak] -> Sanitized Doc + Mapping File
```

Use the sanitized document however you need — send it to an AI tool, review it on a plane, share it as a template. When you need the real names back:

```
Sanitized Doc + Mapping File -> [Uncloak] -> Restored Doc
```

## Use Cases

- **AI-assisted review**: Send sanitized contracts to Claude, ChatGPT, Gemini, or any AI tool for analysis, redlining, or drafting — then uncloak the result
- **Working in public**: Review contracts at a coffee shop, coworking space, or on a flight without exposing client names on your screen
- **Experimenting with new tools**: Try free or low-cost AI tools without worrying about their data retention or training policies
- **Sharing and templating**: Strip client-specific information before sharing work product with colleagues or reusing a contract as a template

## Features

- **Party Name Replacement**: Replace "Acme Corp" -> "[Customer]", "BigCo LLC" -> "[Vendor]" with customizable labels
- **Party Aliases**: Handle parenthetical definitions like `Acme Corp. ("Acme")` — both forms get replaced
- **Filename Sanitization**: Output filenames are scrubbed too — no identity leaks in the file name
- **Security Scanning**: Detects hidden text and prompt injection attempts from opposing counsel
- **Metadata Removal**: Strips author names, company, revision history, timestamps
- **Comment Handling**: Strip, anonymize, or fully sanitize Word comments
- **Bidirectional**: Cloak before sharing, uncloak when you need originals back
- **Format Preservation**: Maintains document formatting, track changes, and comments
- **100% Local**: All processing on your machine. No data sent anywhere.

## Installation

```bash
git clone https://github.com/dfligor/clientcloak.git
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

# Uncloak when you need originals back
clientcloak uncloak redlined.docx --mapping contract_mapping.json

# Security scan only
clientcloak scan contract.docx

# Inspect metadata and comments
clientcloak inspect contract.docx
```

## Workflow

1. **Cloak**: Upload your contract, specify party names, review what will be protected
2. **Download**: Get the sanitized document + mapping file (keep mapping file safe!)
3. **Use it**: Send to an AI tool, review in public, share as a template — whatever you need
4. **Uncloak** (when needed): Upload the document + your mapping file to restore original names
5. **Done**: Get the final document with real names back in place

## Security Note

The mapping file contains the link between placeholders and real client data. Keep it secure and never share it.

## License

MIT
