#!/bin/bash
# Run powt_archive_generator.py over a date range in one-month chunks,
# once per forecast hour.
#
# Usage: ./archive_generator.sh [start_date] [end_date]
#   Dates are YYYY-MM-DD. Defaults: 2024-10-01 to 2025-04-30.
#   Output and log locations come from POWT_OUTPUT_DIR / POWT_LOG_DIR
#   (defaults: ./data/powt and ./logs). SYNOPTIC_TOKEN must be set.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_dir="${POWT_LOG_DIR:-./logs}"
mkdir -p "$log_dir"

# Define start and end dates
start_date="${1:-2024-10-01}"
end_date="${2:-2025-04-30}"

# Define forecast hours
forecast_hours=(36 48 72 120 168)

# Print "chunk_start chunk_end" for each month in the range.
# Uses Python for date math so this works with both GNU and BSD/macOS date.
month_chunks() {
    python3 - "$1" "$2" <<'EOF'
import sys, calendar
from datetime import date
start, end = (date.fromisoformat(d) for d in sys.argv[1:3])
current = start
while current <= end:
    last_day = calendar.monthrange(current.year, current.month)[1]
    chunk_end = min(date(current.year, current.month, last_day), end)
    print(current, chunk_end)
    current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
EOF
}

# Iterate through forecast hours first
for hour in "${forecast_hours[@]}"; do
    # Set interval based on the value of hour
    if [[ "$hour" -le 36 ]]; then
        interval=1
    else
        interval=3
    fi

    # Iterate through dates in one-month chunks
    while read -r chunk_start chunk_end; do
        # Log the Python script call for progress tracking
        echo "python $script_dir/powt_archive_generator.py $chunk_start $chunk_end $hour $interval" \
            > "$log_dir/progress.log"

        # Execute the Python script and log its output
        python "$script_dir/powt_archive_generator.py" \
            "$chunk_start" "$chunk_end" "$hour" "$interval" \
            > "$log_dir/log_${chunk_start}_${chunk_end}_${hour}_${interval}.log"
    done < <(month_chunks "$start_date" "$end_date")
done
