#!/usr/bin/env python3
"""
Publish std_msgs/String "hello world" on /hello_world at 1 Hz.

Requirements (host):
    sudo apt install ros-jazzy-rclpy ros-jazzy-std-msgs
    source /opt/ros/jazzy/setup.bash
"""
import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class HelloPublisher(Node):
    def __init__(self):
        super().__init__("hello_publisher")
        self.pub = self.create_publisher(String, "/hello_world", 10)
        self.timer = self.create_timer(1.0, self._publish)
        self.get_logger().info("Hello publisher started — publishing 'hello world' on /hello_world")

    def _publish(self):
        msg = String()
        msg.data = "hello world"
        self.pub.publish(msg)
        self.get_logger().info("Published: hello world")


def main():
    rclpy.init()
    node = HelloPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
