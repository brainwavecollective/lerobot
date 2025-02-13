#!/bin/bash
echo "
===========================================================
LeRobot USB Physical Port Detection 
===========================================================
NOTICE: I made this for my Raspberry Pi and have made absolutely no effort whatsoever 
to ensure that this works on any other system. 
This was put together as an FYI for another user.

That said, this script is only intended to detect and communicat information, not make system changes. 

In any case, here are the steps that you can perform manually:
1. ls /dev/ttyACM* to find the USB ports (or, use lerobot/scripts/find_motors_bus_port.py)
2. udevadm info -a -n /dev/ttyACM<your port> | grep -m1 KERNELS
3. Create /etc/udev/rules.d/lerobot-arm-config.rules
4. Add KERNELS lines in format: KERNELS==\"YOURINFOGOESHERE\", SYMLINK+=\"ttyACM_LEADER\"
5. sudo udevadm trigger
6. Modify your LeRobot config to use the new symlinks

This script will:
1. Ask you to unplug both devices
2. Guide you through plugging them in one at a time to identify them
3. Generate the correct configuration
4. Give you copy/paste commands to implement the changes
"

# Function to get current TTY list (excluding symlinks)
get_tty_list() {
    ls /dev/ttyACM[0-9] 2>/dev/null
}

# Function to wait for a device to appear
wait_for_device() {
    local timeout=30  # 30 seconds timeout
    local start_time=$(date +%s)
    
    while true; do
        if [ $(date +%s) -gt $((start_time + timeout)) ]; then
            echo "Timeout waiting for device!"
            return 1
        fi
        
        if [ -n "$(get_tty_list)" ]; then
            sleep 1  # Brief pause to ensure device is fully initialized
            return 0
        fi
        
        sleep 0.5
    done
}

# Start with ensuring both devices are unplugged
echo "Please unplug both arms if they are currently connected."
echo "Press Enter when both arms are unplugged..."
read

initial_ttys=$(get_tty_list)
if [ -n "$initial_ttys" ]; then
    echo "ERROR: Still detecting USB devices. Please unplug all LeRobot arms and try again."
    echo "Detected devices:"
    echo "$initial_ttys" | sed 's/^/  /'
    exit 1
fi

# Identify LEADER arm
echo -e "\nPlease plug in the LEADER arm now..."
echo "Press Enter once you've plugged in the LEADER arm..."
read
wait_for_device
leader_device=$(get_tty_list)

if [ -z "$leader_device" ]; then
    echo "Error: Could not detect LEADER arm!"
    exit 1
fi

echo "Detected LEADER arm at: $leader_device"
leader_kernel=$(udevadm info -a -n "$leader_device" | grep -m1 KERNELS | awk -F'"' '{print $2}')

if [ -z "$leader_kernel" ]; then
    echo "Error: Could not get KERNELS info for LEADER arm!"
    exit 1
fi

# Identify FOLLOWER arm
echo -e "\nDetected LEADER arm successfully! Now please plug in the FOLLOWER arm..."
echo "Press Enter once you've plugged in the FOLLOWER arm..."
read
wait_for_device

follower_device=$(get_tty_list | grep -v "$leader_device")

if [ -z "$follower_device" ]; then
    echo "Error: Could not detect FOLLOWER arm!"
    exit 1
fi

echo "Detected FOLLOWER arm at: $follower_device"
follower_kernel=$(udevadm info -a -n "$follower_device" | grep -m1 KERNELS | awk -F'"' '{print $2}')

if [ -z "$follower_kernel" ]; then
    echo "Error: Could not get KERNELS info for FOLLOWER arm!"
    exit 1
fi

# Generate the rules
rules="""# $(basename "$leader_device") is leader, $(basename "$follower_device") is follower
KERNELS==\"$leader_kernel\", SYMLINK+=\"ttyACM_LEADER\"
KERNELS==\"$follower_kernel\", SYMLINK+=\"ttyACM_FOLLOWER\"
"""

echo """

===========================================================
Identification complete! Follow these steps to implement:
===========================================================

*** STEP 1: Create the udev rules file using command:
sudo nano /etc/udev/rules.d/lerobot-arm-config.rules

*** STEP 2: Add the following text to the file:
$rules

*** STEP 3: Reload the udev rules:
sudo udevadm trigger

*** STEP 4: Update your LeRobot configuration to reference the following ports:
/dev/ttyACM_LEADER   (for the leader arm)
/dev/ttyACM_FOLLOWER (for the follower arm)

WARNING: So long as this file is in place, these ports will automatically be identified
in this way. This means that your lerobot arms will now be physically associated with
the current ports. Even if you do not have LeRobot arms plugged in, your ports will
be identified in this way.

To remove this change and go back to standard behavior, simply run:
sudo rm /etc/udev/rules.d/lerobot-arm-config.rules
sudo udevadm trigger
"""
