# Claude Code OCA Proxy

Use [Claude Code](https://claude.com/claude-code) with Oracle Code Assist models.

This proxy allows you to run Claude Code CLI - Anthropic's powerful AI coding assistant - while routing all requests through Oracle Code Assist infrastructure. Get the best of both worlds: Claude Code's excellent user experience with Oracle's enterprise-grade AI models.

## What This Does

Claude Code is an AI-powered command-line tool that helps you write code, debug issues, and understand codebases. Normally, it connects to Anthropic's Claude models. This proxy redirects those connections to use **Oracle Code Assist models** instead, giving you:

- **Enterprise integration** - Use your organization's Oracle Code Assist infrastructure
- **Model flexibility** - Choose from Oracle's available AI models
- **Seamless experience** - Claude Code works exactly as before, just with different backend models
- **Cost control** - Leverage your existing Oracle Code Assist subscription

Simply run this proxy server in the background, and Claude Code automatically uses Oracle Code Assist models.

## Prerequisites

Before you begin, you need:

- **Python 3.8 or newer** installed on your computer
- **Oracle Code Assist access** through your organization
- **Claude Code CLI** - [Download here](https://github.com/anthropics/claude-code)
- **Cline CLI** (for getting Oracle credentials)

## Installation

**Step 1: Download this project**

```bash
git clone https://github.com/yourusername/ClaudeCodeOCA.git
cd ClaudeCodeOCA
```

**Step 2: Set up Python environment**

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

**Step 3: Install required packages**

```bash
pip install -r requirements.txt
```

## Getting Your Oracle Credentials

You'll need Oracle Code Assist credentials to use this proxy. The easiest way to get them is through Cline:

1. **Install and run Cline CLI** (if you don't have it already)

2. **Authenticate with Oracle**:
   ```bash
   cline auth
   ```

3. **Sign in** using your Oracle Single Sign-On (SSO) account

4. **Find your credentials**: After signing in, Cline creates a credentials file at:
   ```
   ~/.cline/data/secrets.json
   ```

5. **Copy the credentials** to this project folder (or skip this step and pass in the token location when running the proxy):
   ```bash
   cp ~/.cline/data/secrets.json /path/to/ClaudeCodeOCA/secrets.json
   ```

That's it! The `secrets.json` file contains your Oracle authentication tokens.

## Running the Proxy

**Basic start** (if `secrets.json` is in the project folder):

```bash
python claude_code_proxy.py
```

**Specify credential file location**:

```bash
python claude_code_proxy.py --token ~/.cline/data/secrets.json
```

**Choose specific Oracle models**:

```bash
python claude_code_proxy.py \
  --small-model oca/grok-code-fast-1 \
  --big-model oca/grok-code-fast-1 \
  --port 8080
```

You should see:
```
Successfully loaded new access token from secrets.json
Starting server on :8080
```

The proxy is now running! Keep this terminal window open.

## Using Claude Code with the Proxy

Open a **new terminal window** and run Claude Code with this environment variable:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

That's it! Claude Code now uses Oracle Code Assist models for all AI requests.

**To make this permanent**, add to your `~/.bashrc` or `~/.zshrc`:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
```

Then you can just run `claude` normally.

## Command-Line Options

Customize the proxy with these options:

| Option | Default | Description |
|--------|---------|-------------|
| `--token` | `secrets.json` | Path to your Oracle credentials file |
| `--small-model` | `oca/grok-code-fast-1` | Oracle model for faster/smaller requests |
| `--big-model` | `oca/grok-code-fast-1` | Oracle model for complex/larger requests |
| `--port` | `8080` | Port number the proxy listens on |
| `--log-level` | `info` | Logging detail: `debug`, `info`, `warn`, or `error` |

**Example with custom settings**:

```bash
python claude_code_proxy.py \
  --token ~/my-tokens/oracle-creds.json \
  --small-model oca/fast-model \
  --big-model oca/powerful-model \
  --port 8080 \
  --log-level debug
```

## Verifying It Works

**Check if the proxy is running**:

```bash
curl http://localhost:8080/health
```

Should return:
```json
{"status":"healthy"}
```

**Test with Claude Code**:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Then ask Claude to do something simple like "Hello, can you help me write a Python function?"

If you see a response, it's working! That response came from Oracle Code Assist.

## Troubleshooting

### "FATAL: Could not perform initial load of token file"

**Problem**: The proxy can't find or read your credentials file.

**Solutions**:
- Check that `secrets.json` exists in the project folder
- Or use `--token` to specify the correct path
- Verify the file isn't empty or corrupted

### "ERROR: API key not loaded"

**Problem**: The credentials file doesn't have the expected format.

**Solutions**:
- Re-run `cline auth` to get fresh credentials
- Copy the file again from `~/.cline/data/secrets.json`
- Make sure you copied the entire file content

### "Connection refused" when running Claude Code

**Problem**: Claude Code can't reach the proxy server.

**Solutions**:
- Make sure the proxy is running (check the terminal where you started it)
- Verify you're using the correct port (default is 8080)
- Try `curl http://localhost:8080/health` to test connectivity

### "Oracle error: 401" (Unauthorized)

**Problem**: Your credentials have expired.

**Solutions**:
- Run `cline auth` again to refresh your credentials
- Copy the new `secrets.json` file to the project folder
- The proxy will automatically reload the new credentials within 10 seconds

### "Oracle error: 500" (Server Error)

**Problem**: Issue with Oracle Code Assist service.

**Solutions**:
- Check with your organization's Oracle Code Assist administrator
- Verify the model names you're using are available
- Try again in a few minutes (temporary service issue)

### Want more details?

Enable debug logging to see exactly what's happening:

```bash
python claude_code_proxy.py --log-level debug
```

This shows detailed information about every request and response.

## How It Works (Technical Details)

For those interested in the technical implementation:

### Architecture Overview

```
Claude Code CLI ──> Anthropic API Format ──> Proxy ──> Oracle Code Assist ──> Response
                    (Messages API)          (Converts)  (Chat Completions API)
```

The proxy acts as a translator between two different API formats:

1. **Claude Code** sends requests in Anthropic's Messages API format
2. **Proxy** converts these to OpenAI Chat Completions format (used by Oracle)
3. **Oracle Code Assist** processes the request and streams back responses
4. **Proxy** converts the response back to Anthropic's format
5. **Claude Code** receives the response and displays it to you

### Key Technical Features

- **API Translation**: Converts between Anthropic Messages API and OpenAI Chat Completions format
- **Streaming Support**: Full support for Server-Sent Events (SSE) streaming and non-streaming modes
- **Tool Use**: Complete support for Claude's tool/function calling capabilities, including parallel tool execution
- **Token Management**: Background thread monitors credentials file and auto-reloads when updated (checks every 10 seconds)
- **Model Mapping**: Automatically maps Claude model names (like "haiku" or "sonnet") to your configured Oracle models
- **Async Architecture**: Built with Python's async/await for high performance and concurrent request handling

### What Gets Converted

**Message Format**: User/assistant messages, system prompts, and conversation history

**Tool Calls**: Claude's tool use blocks are converted to OpenAI-style function calling

**Streaming Events**: Real-time response streaming with proper event formatting

**Usage Metrics**: Token counts and usage statistics

**Error Handling**: HTTP errors, timeouts, and API issues are properly handled and reported

### API Endpoints

The proxy exposes these endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/messages` | Main API endpoint (handles all Claude Code requests) |
| `GET` | `/health` | Health check - returns `{"status":"healthy"}` |
| `GET` | `/` | Information endpoint |

### Automatic Token Refresh

The proxy watches your credentials file and reloads it automatically when it changes. This means:

- If your Oracle tokens expire, just run `cline auth` again
- The proxy detects the file update within 10 seconds
- No need to restart the proxy server
- Seamless credential rotation

## Project Structure

```
ClaudeCodeOCA/
├── claude_code_proxy.py     # Main proxy server (1,052 lines)
├── requirements.txt          # Python dependencies
├── secrets.json.example      # Example credentials format
├── .gitignore               # Git exclusions
├── LICENSE                  # MIT License
└── README.md                # This file
```

## Limitations

Current limitations of this proxy:

- **Single-process**: Each proxy instance handles requests sequentially. For high concurrency, run multiple instances.
- **No built-in authentication**: Assumes trusted local network. Add authentication for production use.
- **No rate limiting**: Requests are forwarded immediately without throttling.
- **Token cap**: Maximum 16,384 tokens per request (configurable in code).
- **Hardcoded endpoint**: Oracle endpoint URL is in the source code (line 29).

## Contributing

Contributions are welcome! To contribute:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to your branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Anthropic](https://www.anthropic.com/) for Claude and the Claude Code CLI
- [Oracle](https://www.oracle.com/) for Oracle Code Assist
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
- [httpx](https://www.python-httpx.org/) for async HTTP capabilities

## Support

For issues, questions, or contributions:
- **Issues**: [GitHub Issues](https://github.com/yourusername/ClaudeCodeOCA/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/ClaudeCodeOCA/discussions)

## Disclaimer

This project is not officially affiliated with, endorsed by, or supported by Anthropic or Oracle. Ensure compliance with the terms of service for both Claude API and Oracle Code Assist.
