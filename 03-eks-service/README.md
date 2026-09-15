# Lab 03: Run the mortgage assistant on EKS

This lab packages the Strands application as a FastAPI service and deploys it
to the EKS cluster created in Lab 00.

Deploy or update the application:

```bash
./scripts/deploy-application.sh --region us-west-2
```

Send additional prompts without rebuilding the image:

```bash
python3 app/invoke_eks.py \
  --prompt "When does refinancing make sense?"
```

Application images are rebuilt only when you rerun `deploy-application.sh`.
Normal prompts are HTTP requests to the already-running service.

Check the deployment:

```bash
kubectl get deployment,pods,service --namespace mortgage-assistant
kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant --tail=200
```
