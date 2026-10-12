#!/usr/bin/env bash
# =============================================================================
# Smoke tests for the pj-bridge snap — full end-to-end bridge verification
#
# Pipeline:
#   [ros2cli snap] ros2 topic pub  →  DDS (UDP)  →  [snap] pj-bridge-ros2
#                                                       ↓ WebSocket :9090
#                                               [host Python] ws_reader.py
#                                                       ↓
#                                               assert "hello world" decoded
#
# Requirements on the host:
#   sudo snap install ros2cli   (host-side ROS 2 publisher)
#   pip3 install websockets zstandard is NOT needed — the script creates a venv automatically
#
# Usage:
#   bash tests/integration/smoke_tests.sh
# =============================================================================
set -e

PROJECT_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
TEST_DIR="$PROJECT_ROOT/tests/integration"
SNAP_FILE="$(ls "$PROJECT_ROOT"/pj-bridge_*.snap 2>/dev/null | head -1 || true)"
WS_PORT=9090
FASTRTPS_PROFILE="$HOME/.config/pj-bridge/fastrtps_no_shm.xml"

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [ -z "$SNAP_FILE" ]; then
  echo "ERROR: no pj-bridge_*.snap found in $PROJECT_ROOT"
  echo "       Build it first with: snapcraft pack"
  exit 1
fi
echo "Snap file: $SNAP_FILE"

# ---------------------------------------------------------------------------
# FastRTPS UDP-only profile
# Disables shared-memory transport so host-side DDS can cross the snap
# AppArmor/mount namespace boundary — same technique used by termviz2 tests.
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$FASTRTPS_PROFILE")"
cat > "$FASTRTPS_PROFILE" << 'EOF'
<?xml version="1.0" encoding="UTF-8" ?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
    <transport_descriptors>
        <transport_descriptor>
            <transport_id>UDPv4Transport</transport_id>
            <type>UDPv4</type>
        </transport_descriptor>
    </transport_descriptors>
    <participant profile_name="default_xrce_dds_profile" is_default_profile="true">
        <rtps>
            <userTransports>
                <transport_id>UDPv4Transport</transport_id>
            </userTransports>
            <useBuiltinTransports>false</useBuiltinTransports>
        </rtps>
    </participant>
</profiles>
EOF
echo "FastRTPS UDP-only profile written to $FASTRTPS_PROFILE"

# ---------------------------------------------------------------------------
# Install snap
# ---------------------------------------------------------------------------
echo ""
echo "=== Installing snap ==="
sudo snap install --dangerous "$SNAP_FILE"
sleep 3   # let snapd activate AppArmor/mount namespaces

echo ""
echo "=== snap list ==="
snap list pj-bridge

echo ""
echo "=== snap connections ==="
snap connections pj-bridge

# ---------------------------------------------------------------------------
# Python venv for the ws_reader dependencies (websockets + zstandard).
# ---------------------------------------------------------------------------
VENV_DIR="/tmp/pj_bridge_test_venv"
echo ""
echo "=== Setting up Python venv ($VENV_DIR) ==="
python3 -m venv --clear "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q -r "$TEST_DIR/requirements.txt"
echo "  Dependencies installed: $("$VENV_DIR/bin/pip" freeze | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# Install the ros2cli snap (host-side ROS 2 publisher) and point its DDS at
# the UDP-only profile so it crosses the snap namespace boundary.
# The profile must be a non-hidden path for the snap's 'home' interface.
# ---------------------------------------------------------------------------
if ! snap list ros2cli >/dev/null 2>&1; then
  echo "Installing ros2cli snap..."
  sudo snap install ros2cli
fi
cp "$FASTRTPS_PROFILE" "$HOME/fastrtps_no_shm.xml"
export FASTRTPS_DEFAULT_PROFILES_FILE="$HOME/fastrtps_no_shm.xml"

# ---------------------------------------------------------------------------
# Start the bridge snap
# ---------------------------------------------------------------------------
echo ""
echo "=== Starting pj-bridge snap ==="
snap run pj-bridge.pj-bridge-ros2 --ros-args -p port:="$WS_PORT" \
    > /tmp/pj_bridge.log 2>&1 &
BRIDGE_PID=$!
echo "  Bridge PID: $BRIDGE_PID"
sleep 5   # let ROS 2 init and WebSocket server bind

# Check it's still alive
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
  echo "ERROR: pj-bridge-ros2 exited immediately — see /tmp/pj_bridge.log"
  cat /tmp/pj_bridge.log
  exit 1
fi
echo "  Bridge is running."

# ---------------------------------------------------------------------------
# Start the hello-world publisher (host side)
# ---------------------------------------------------------------------------
echo ""
echo "=== Starting hello world publisher (ros2cli snap) ==="
ros2cli.ros2 topic pub -r 1 /hello_world std_msgs/msg/String \
    "data: 'hello world'" > /tmp/pub_hello.log 2>&1 &
PUB_PID=$!
echo "  Publisher PID: $PUB_PID"
sleep 3   # let DDS participant discovery complete

# ---------------------------------------------------------------------------
# Run the WebSocket reader
# Connects to the bridge, subscribes to /hello_world, decodes the CDR-
# serialized std_msgs/String and asserts it contains "hello".
# ---------------------------------------------------------------------------
echo ""
echo "=== Running WebSocket reader (timeout 20 s) ==="
set +e
"$VENV_DIR/bin/python3" "$TEST_DIR/ws_reader.py" \
    --url "ws://localhost:$WS_PORT" \
    --topic /hello_world \
    --timeout 20
READER_EXIT=$?
set -e

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
echo ""
echo "=== Cleaning up ==="
kill "$BRIDGE_PID" "$PUB_PID" 2>/dev/null || true
sleep 1
kill -9 "$BRIDGE_PID" "$PUB_PID" 2>/dev/null || true
wait "$BRIDGE_PID" "$PUB_PID" 2>/dev/null || true

echo ""
echo "--- Bridge log (/tmp/pj_bridge.log) ---"
cat /tmp/pj_bridge.log

echo ""
echo "--- Publisher log (/tmp/pub_hello.log) ---"
cat /tmp/pub_hello.log

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
echo ""
if [ "$READER_EXIT" -eq 0 ]; then
  echo "=== All smoke tests PASSED ==="
else
  echo "FAIL: ws_reader.py exited $READER_EXIT — 'hello world' was not received through the bridge"
  exit 1
fi
