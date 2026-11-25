# 🚀 Rapid Radar - Getting Started Checklist

## Step 1: Install Dependencies ✅

Run the installation script:
```bash
install.bat
```

Wait for:
- ✅ Python package installation
- ✅ Flask installation
- ✅ Playwright browser downloads (Chrome, Firefox, Edge)

---

## Step 2: Start the Web Interface ✅

Double-click:
```
start_web_ui.bat
```

Or run manually:
```bash
python app.py
```

You should see:
```
🚀 Rapid Radar - Performance Testing Framework
   Created by OPUS EDM PERF TEAM
=================================================================
   Server starting on http://127.0.0.1:5000
```

---

## Step 3: Open in Browser ✅

Navigate to:
```
http://127.0.0.1:5000
```

You should see the Rapid Radar web interface with:
- 🚀 Rapid Radar header
- "Created by OPUS EDM PERF TEAM" subtitle
- Form with all input fields
- Live log viewer on the right

---

## Step 4: Run Your First Test ✅

Fill in the form:

| Field | Example Value |
|-------|---------------|
| Website Login Page | https://myapp.example.com/login |
| Username | testuser@example.com |
| Password | your_password |
| Test URL | https://myapp.example.com/dashboard |
| Browsers | ✅ Chrome (at minimum) |
| Output Folder | reports |

Click **Submit** and watch the logs stream in real-time!

---

## Step 5: View Results ✅

Once the test completes, check the output folder:

```
reports/
└── Dashboard/
    ├── Dashboard_chrome.html
    ├── Dashboard_edge.html (if selected)
    └── Dashboard_firefox.html (if selected)
```

Open any HTML file in your browser to see the detailed performance report!

---

## Troubleshooting

### ❌ "Module not found" error
→ Run `install.bat` again

### ❌ Port 5000 in use
→ Change port in `app.py` (line with `port=5000`)

### ❌ Browsers not downloading
→ Run manually: `python -m playwright install`

### ❌ Page not loading
→ Check that `templates/index.html` exists

---

## Files Created

✅ **app.py** - Flask web server with real-time logging
✅ **templates/index.html** - Web interface with form and log viewer
✅ **start_web_ui.bat** - Quick launcher script
✅ **WEB_UI_GUIDE.md** - Comprehensive user guide
✅ **GETTING_STARTED.md** - This checklist
✅ **requirements.txt** - Updated with Flask dependency
✅ **install.bat** - Updated to install Flask
✅ **README.md** - Updated with web UI instructions

---

## Next Steps

1. ✅ Bookmark `http://127.0.0.1:5000` for easy access
2. ✅ Read `WEB_UI_GUIDE.md` for detailed usage instructions
3. ✅ Run tests on your target applications
4. ✅ Share reports with your team!

---

**Created by OPUS EDM PERF TEAM** 🚀
