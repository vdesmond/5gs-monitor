# Bring-up order matters: monitoring first (the Open5GS charts render ServiceMonitor CRDs).
#   make up        everything, in order
#   make down      remove the 5gs namespace and its Helm releases (monitoring stays)
#   make status    pods + slice registration state
#   make experiment  A/B run (results/<stamp>/), then `make plot`
CHARTS   := third_party/5g-charts/charts
NS       := 5gs
IMAGE    := 5gs-monitor:local
KPS_VER  := 91.4.1

.PHONY: up monitoring image core ran controller down status experiment plot grafana clean

up: monitoring image core ran controller

monitoring:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
	helm upgrade --install kps prometheus-community/kube-prometheus-stack --version $(KPS_VER) \
	    -n monitoring --create-namespace -f deploy/monitoring/values.yaml >/dev/null
	kubectl create configmap fivegs-monitor-dashboard -n monitoring \
	    --from-file=dashboard.json=deploy/monitoring/dashboard.json --dry-run=client -o yaml \
	    | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f - >/dev/null
	kubectl -n monitoring rollout status deploy/kps-operator --timeout=180s >/dev/null

image:
	docker build -q -t $(IMAGE) .

core:
	for c in open5gs open5gs-smf open5gs-upf; do helm dependency build $(CHARTS)/$$c >/dev/null; done
	kubectl apply -f deploy/core/namespace.yaml -f deploy/core/mongodb.yaml >/dev/null
	helm upgrade --install open5gs   $(CHARTS)/open5gs     -n $(NS) -f deploy/core/open5gs-values.yaml   >/dev/null
	helm upgrade --install smf-urllc $(CHARTS)/open5gs-smf -n $(NS) -f deploy/core/smf-urllc-values.yaml >/dev/null
	helm upgrade --install upf-urllc $(CHARTS)/open5gs-upf -n $(NS) -f deploy/core/upf-urllc-values.yaml >/dev/null
	kubectl apply -f deploy/core/servicemonitor.yaml -f deploy/core/upf-embb-shaper-service.yaml >/dev/null
	kubectl -n $(NS) rollout status deploy/open5gs-mongodb --timeout=180s >/dev/null
	kubectl delete job -n $(NS) provision --ignore-not-found >/dev/null
	kubectl apply -f deploy/core/provision-job.yaml >/dev/null
	kubectl -n $(NS) wait --for=condition=complete job/provision --timeout=180s >/dev/null
	kubectl -n $(NS) rollout status deploy/open5gs-amf --timeout=180s >/dev/null

ran:
	kubectl apply -f deploy/ran/ >/dev/null
	kubectl -n $(NS) rollout status deploy/gnb --timeout=180s >/dev/null
	kubectl -n $(NS) rollout status deploy/ue-embb deploy/ue-urllc --timeout=180s >/dev/null

controller:
	kubectl create configmap controller-policy -n $(NS) \
	    --from-file=policy.yaml=deploy/controller/policy.yaml --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	kubectl apply -f deploy/controller/controller.yaml >/dev/null
	kubectl -n $(NS) rollout restart deploy/controller >/dev/null
	kubectl -n $(NS) rollout status deploy/controller --timeout=120s >/dev/null

down:
	helm uninstall open5gs smf-urllc upf-urllc -n $(NS) >/dev/null 2>&1 || true
	kubectl delete namespace $(NS) --ignore-not-found

status:
	kubectl get pods -n $(NS)
	@for s in embb urllc; do printf "ue-%s: " $$s; \
	  kubectl -n $(NS) logs deploy/ue-$$s -c nr-ue 2>/dev/null | grep -oE 'TUN interface\[uesimtun0, [0-9.]+\] is up' | tail -1; done

experiment:   # caffeinate: a sleeping Mac pauses the OrbStack VM mid-run
	$(shell command -v caffeinate >/dev/null && echo caffeinate -i) uv run experiments/run.py

plot:
	uv run --extra analysis experiments/plot.py

grafana:
	@echo "http://localhost:3000  (admin / admin)"
	kubectl -n monitoring port-forward svc/kps-grafana 3000:80

clean:
	rm -rf .venv results/*/kpis.csv.tmp
