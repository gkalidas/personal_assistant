# Ollama Configuration

Run these commands once to configure Ollama for better memory usage:

```bash
# Create a systemd drop-in override
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf << 'EOF'
[Service]
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF

# Apply and restart
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

And for swap preference:
```bash
echo 'vm.swappiness=80' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

## What these do
- `OLLAMA_KEEP_ALIVE=5m` — unloads model from RAM after 5 min of inactivity (instead of keeping it forever)
- `OLLAMA_MAX_LOADED_MODELS=1` — only one model in RAM at a time (our usage is always single-model)
- `OLLAMA_NUM_PARALLEL=1` — one inference at a time (single user, prevents RAM overuse)
- `vm.swappiness=80` — kernel prefers swapping over OOM-killing; with 22GB swap this is safe
