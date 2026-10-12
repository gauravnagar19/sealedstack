output "ip" {
  description = "Public IPv4 address of the new instance."
  value       = google_compute_instance.this.network_interface[0].access_config[0].nat_ip
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
