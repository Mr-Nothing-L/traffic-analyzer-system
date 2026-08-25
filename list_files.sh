#!/bin/bash
find . -type f -iname "*.mp4" 2>/dev/null | grep -v ".git"
echo "---output---"
ls -la output/ 2>/dev/null
echo "---agent---"
ls -la agent/ 2>/dev/null | head -30
