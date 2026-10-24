# Add to your Fastfile. Every release lane calls `qa_gate` before uploading —
# no build reaches TestFlight / Play unless the agent passed it.

desc "Run the autonomous QA agent against the freshly built binary; abort the lane if it blocks"
lane :qa_gate do |options|
  platform = options[:platform]           # "ios" | "android"
  cfg = "qa/app.#{platform}.yaml"
  sh("mobileqa doctor #{cfg}") rescue UI.important("doctor reported problems — continuing")
  sh("mobileqa ci #{cfg} --steps #{options[:steps] || 400}") do |status, _out, _cmd|
    UI.user_error!("mobileqa blocked the release — see reports/latest/report.html") unless status.success?
  end
end

# Example:
#
# platform :android do
#   lane :release do
#     gradle(task: "assembleDebug")                    # test binary for the agent
#     qa_gate(platform: "android")
#     gradle(task: "bundleRelease")
#     upload_to_play_store(track: "internal")
#   end
# end
#
# platform :ios do
#   lane :release do
#     build_app(scheme: "App", sdk: "iphonesimulator", skip_archive: true, skip_codesigning: true)
#     qa_gate(platform: "ios")
#     build_app(scheme: "App")
#     upload_to_testflight
#   end
# end
